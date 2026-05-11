"""
Qwen 情感 LoRA 微调脚本（仅对「最后一轮 Assistant 回复」计算损失）。

改进点摘要：
- 使用 tokenizer offset_mapping 对齐 mask，避免前缀单独 encode 与整句 tokenize 不一致；
- 自定义 DataCollator，保留数据集内的 labels，不再被 DataCollatorForLanguageModeling 覆盖；
- map 阶段返回 Python list，不在 Dataset.map 中使用 return_tensors='pt'；
- 非量化 fp16 模型不再调用 prepare_model_for_kbit_training；
- 支持 argparse：模型路径、CSV、输出目录、样本上限等；
- 验证集划分尽量 stratify，类别过少时自动回退。
"""
from __future__ import annotations

import argparse
import inspect
import os
import warnings
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd
import torch
from datasets import Dataset
from peft import LoraConfig, TaskType, get_peft_model
from sklearn.model_selection import train_test_split
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    PreTrainedTokenizerBase,
    Trainer,
    TrainingArguments,
)

# 仅忽略已知嘈杂告警，便于发现真正的训练错误
warnings.filterwarnings(
    "ignore",
    message=".*torch.utils.checkpoint.*",
    category=UserWarning,
)


ASSISTANT_MARKER = "Assistant: "


def parse_args():
    p = argparse.ArgumentParser(description="LoRA 微调 Qwen 情感分类（因果 LM）")
    p.add_argument(
        "--model_path",
        type=str,
        default=os.environ.get("QWEN_MODEL_PATH", "/root/code/Finance/Qwen"),
        help="基座模型目录或 HuggingFace id",
    )
    p.add_argument(
        "--csv_path",
        type=str,
        default="nasdaq_news_sentiment/sentiment_deepseek_new_cleaned_nasdaq_news_full.csv",
        help="训练 CSV 路径",
    )
    p.add_argument(
        "--output_dir",
        type=str,
        default="./qwen_sentiment_model",
        help="LoRA 输出目录",
    )
    p.add_argument(
        "--max_samples",
        type=int,
        default=None,
        help="最多使用前 N 条样本（默认 None 使用全部，调试可设如 1000）",
    )
    p.add_argument("--max_length", type=int, default=512)
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--batch_size", type=int, default=4)
    p.add_argument("--grad_accum", type=int, default=4)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--gradient_checkpointing",
        action="store_true",
        help="开启梯度检查点以省显存（推荐大模型/长序列）",
    )
    return p.parse_args()


def load_and_preprocess_data(csv_path: str, max_samples: Optional[int]) -> pd.DataFrame:
    print("正在加载数据...")
    df = pd.read_csv(csv_path)
    if max_samples is not None:
        df = df.iloc[:max_samples]
        print(f"使用样本上限: max_samples={max_samples}")

    df = df[df["Lsa_summary"].notna() & df["sentiment_deepseek"].notna()]
    df = df[df["sentiment_deepseek"] != 0]

    print(f"有效数据数量: {len(df)}")
    print(f"情感分布:\n{df['sentiment_deepseek'].value_counts().sort_index()}")
    return df


def create_prompt_template(text, sentiment, stock_symbol="STOCK") -> str:
    """与训练数据一致的对话格式（可根据业务替换 few-shot 示例）。"""
    system_prompt = (
        "You are a financial analyst. Given a news summary related to a stock, "
        "output a single sentiment score from 1 to 5 (1=most negative, 5=most positive). "
        "Respond only with the score in the Assistant line as in the examples."
    )
    user_content = f"News to Stock Symbol -- {stock_symbol}: {text}"
    conversation = f"""System: {system_prompt}

User: News to Stock Symbol -- AAPL: Apple (AAPL) increase 22%
Assistant: 5

User: News to Stock Symbol -- AAPL: Apple (AAPL) price decreased 30%
Assistant: 1

User: News to Stock Symbol -- AAPL: Apple (AAPL) announced iPhone 15
Assistant: 4

User: {user_content}
Assistant: {sentiment}"""
    return conversation


def _labels_from_offsets(
    input_ids: List[int],
    offsets: List[tuple],
    answer_char_start: int,
) -> List[int]:
    """仅在「最后一轮 Assistant 回复」的 token 上计算 LM loss，其余为 -100。"""
    labels: List[int] = []
    for i, tid in enumerate(input_ids):
        start, end = offsets[i]
        if start == 0 and end == 0:
            # 特殊 token（部分 tokenizer）
            labels.append(-100)
        elif end <= answer_char_start:
            labels.append(-100)
        elif start >= answer_char_start:
            labels.append(tid)
        else:
            # 跨越边界的 token：保留损失，避免答案首 token 被整块 mask
            labels.append(tid)
    return labels


def _tokenize_one(
    tokenizer: PreTrainedTokenizerBase,
    text: str,
    max_length: int,
) -> Dict[str, Any]:
    """单条样本：返回 input_ids / attention_mask / labels（list[int]）。"""
    pos = text.rfind(ASSISTANT_MARKER)
    if pos == -1:
        answer_char_start = len(text)
    else:
        answer_char_start = pos + len(ASSISTANT_MARKER)

    use_offsets = getattr(tokenizer, "is_fast", False)

    if use_offsets:
        enc = tokenizer(
            text,
            truncation=True,
            max_length=max_length,
            padding=False,
            return_offsets_mapping=True,
            add_special_tokens=True,
        )
        input_ids = enc["input_ids"]
        attention_mask = enc["attention_mask"]
        offsets = enc["offset_mapping"]
        labels = _labels_from_offsets(input_ids, offsets, answer_char_start)
    else:
        enc = tokenizer(
            text,
            truncation=True,
            max_length=max_length,
            padding=False,
            add_special_tokens=True,
        )
        input_ids = enc["input_ids"]
        attention_mask = enc["attention_mask"]
        prefix_text = text[:answer_char_start] if pos != -1 else ""
        prefix_enc = tokenizer(
            prefix_text,
            truncation=True,
            max_length=max_length,
            padding=False,
            add_special_tokens=True,
        )
        prefix_ids = prefix_enc["input_ids"]
        mask_end = 0
        for a, b in zip(prefix_ids, input_ids):
            if a == b:
                mask_end += 1
            else:
                break
        labels = [-100] * mask_end + input_ids[mask_end:]
        if len(labels) != len(input_ids):
            labels = [-100] * len(input_ids)

    # 训练时常用右侧 padding；动态 pad 在 collator 中完成
    assert len(input_ids) == len(labels) == len(attention_mask)
    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
    }


def prepare_dataset(
    df: pd.DataFrame,
    tokenizer: PreTrainedTokenizerBase,
    max_length: int,
    seed: int,
):
    texts: List[str] = []
    cls_labels: List[int] = []

    for _, row in df.iterrows():
        raw = row["Lsa_summary"]
        sentiment = int(row["sentiment_deepseek"])
        stock_symbol = row.get("Stock_symbol", "STOCK")
        if pd.isna(raw) or raw == "":
            continue
        texts.append(create_prompt_template(raw, sentiment, stock_symbol))
        cls_labels.append(sentiment)

    # 分层划分：类别样本过少时 sklearn 会报错，自动回退
    stratify = cls_labels if len(set(cls_labels)) > 1 else None
    if stratify is not None:
        counts = pd.Series(cls_labels).value_counts()
        if counts.min() < 2:
            stratify = None
            print("警告: 某情感类别样本 < 2，无法 stratify，改用随机划分")

    try:
        split = train_test_split(
            texts,
            cls_labels,
            test_size=0.2,
            random_state=seed,
            stratify=stratify,
        )
        train_texts, eval_texts, _, _ = split
    except ValueError as e:
        print(f"stratify 失败 ({e})，改用随机划分")
        train_texts, eval_texts, _, _ = train_test_split(
            texts, cls_labels, test_size=0.2, random_state=seed
        )

    print(f"训练集大小: {len(train_texts)}, 验证集大小: {len(eval_texts)}")

    train_ds = Dataset.from_dict({"text": train_texts})
    eval_ds = Dataset.from_dict({"text": eval_texts})

    def tokenize_batch(examples: Dict[str, List[str]]) -> Dict[str, List]:
        batch_ids, batch_mask, batch_labels = [], [], []
        for t in examples["text"]:
            one = _tokenize_one(tokenizer, t, max_length)
            batch_ids.append(one["input_ids"])
            batch_mask.append(one["attention_mask"])
            batch_labels.append(one["labels"])
        return {
            "input_ids": batch_ids,
            "attention_mask": batch_mask,
            "labels": batch_labels,
        }

    rm_cols = train_ds.column_names
    train_tok = train_ds.map(tokenize_batch, batched=True, remove_columns=rm_cols)
    eval_tok = eval_ds.map(tokenize_batch, batched=True, remove_columns=eval_ds.column_names)
    return train_tok, eval_tok


@dataclass
class DataCollatorForCompletionLM:
    """保留每条样本的 labels（含 -100 mask），动态 pad 到 batch 内最大长度。"""

    tokenizer: PreTrainedTokenizerBase
    pad_to_multiple_of: Optional[int] = None

    def __call__(self, features: List[Dict[str, Any]]) -> Dict[str, torch.Tensor]:
        pad_id = self.tokenizer.pad_token_id
        max_len = max(len(f["input_ids"]) for f in features)
        if self.pad_to_multiple_of:
            m = self.pad_to_multiple_of
            max_len = ((max_len + m - 1) // m) * m

        batch_input_ids: List[List[int]] = []
        batch_attention_mask: List[List[int]] = []
        batch_labels: List[List[int]] = []

        for f in features:
            ids = list(f["input_ids"])
            attn = list(f["attention_mask"])
            lbl = list(f["labels"])
            assert len(ids) == len(attn) == len(lbl)
            pad_len = max_len - len(ids)
            if pad_len > 0:
                ids = ids + [pad_id] * pad_len
                attn = attn + [0] * pad_len
                lbl = lbl + [-100] * pad_len
            batch_input_ids.append(ids)
            batch_attention_mask.append(attn)
            batch_labels.append(lbl)

        return {
            "input_ids": torch.tensor(batch_input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(batch_attention_mask, dtype=torch.long),
            "labels": torch.tensor(batch_labels, dtype=torch.long),
        }


def create_model_and_tokenizer(
    model_path: str,
    gradient_checkpointing: bool,
):
    print(f"正在加载模型和分词器: {model_path}")

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    # 因果 LM 微调常用右填充，便于 batch 内对齐 completion
    tokenizer.padding_side = "right"

    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )

    if gradient_checkpointing:
        model.gradient_checkpointing_enable()
        model.config.use_cache = False

    # 非量化模型不要使用 prepare_model_for_kbit_training

    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.1,
        target_modules=[
            "q_proj",
            "v_proj",
            "k_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
        bias="none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    return model, tokenizer


def train_model(
    model,
    tokenizer,
    train_dataset,
    eval_dataset,
    output_dir: str,
    args: argparse.Namespace,
):
    print("开始训练模型...")
    collator = DataCollatorForCompletionLM(tokenizer)

    # 旧版 transformers 使用 evaluation_strategy，新版为 eval_strategy
    sched_param = (
        "eval_strategy"
        if "eval_strategy" in inspect.signature(TrainingArguments.__init__).parameters
        else "evaluation_strategy"
    )
    train_kw = {
        "output_dir": output_dir,
        "num_train_epochs": args.epochs,
        "per_device_train_batch_size": args.batch_size,
        "gradient_accumulation_steps": args.grad_accum,
        "warmup_steps": 100,
        "learning_rate": args.lr,
        "fp16": torch.cuda.is_available(),
        "logging_steps": 50,
        "save_steps": 500,
        "eval_steps": 500,
        sched_param: "steps",
        "save_strategy": "steps",
        "load_best_model_at_end": True,
        "metric_for_best_model": "eval_loss",
        "greater_is_better": False,
        "dataloader_pin_memory": torch.cuda.is_available(),
        "remove_unused_columns": False,
        "gradient_checkpointing": args.gradient_checkpointing,
    }
    training_args = TrainingArguments(**train_kw)

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=collator,
        tokenizer=tokenizer,
    )
    trainer.train()
    trainer.save_model()
    tokenizer.save_pretrained(output_dir)
    print(f"模型已保存到: {output_dir}")


def main():
    args = parse_args()
    df = load_and_preprocess_data(args.csv_path, args.max_samples)
    if len(df) < 4:
        raise SystemExit("有效样本过少，无法划分训练/验证集，请检查 CSV 与过滤条件")

    model, tokenizer = create_model_and_tokenizer(
        args.model_path,
        args.gradient_checkpointing,
    )
    train_ds, eval_ds = prepare_dataset(df, tokenizer, args.max_length, args.seed)
    train_model(model, tokenizer, train_ds, eval_ds, args.output_dir, args)
    print("训练完成！")


if __name__ == "__main__":
    main()
