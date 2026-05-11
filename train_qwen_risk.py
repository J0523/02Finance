"""
Qwen 风险打分 LoRA 微调（仅对「最后一轮 Assistant 回复」计算损失）。

与 train_qwen_sentiment.py 对齐的改进：
- offset_mapping / slow tokenizer 回退对齐 labels mask；
- 自定义 DataCollatorForCompletionLM，保留 labels；
- Dataset.map 返回 list，不使用 return_tensors='pt'；
- 非量化 fp16 不调用 prepare_model_for_kbit_training；
- argparse、stratify 划分、TrainingArguments eval_strategy 兼容。
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

warnings.filterwarnings(
    "ignore",
    message=".*torch.utils.checkpoint.*",
    category=UserWarning,
)

ASSISTANT_MARKER = "Assistant: "


def parse_args():
    p = argparse.ArgumentParser(description="LoRA 微调 Qwen 风险打分（因果 LM）")
    p.add_argument(
        "--model_path",
        type=str,
        default=os.environ.get("QWEN_MODEL_PATH", "/root/code/Finance/Qwen"),
        help="基座模型目录或 HuggingFace id",
    )
    p.add_argument(
        "--csv_path",
        type=str,
        default="risk_nasdaq/risk_deepseek_cleaned_nasdaq_news_full.csv",
        help="训练 CSV 路径",
    )
    p.add_argument(
        "--output_dir",
        type=str,
        default="./qwen_risk_model",
        help="LoRA 输出目录",
    )
    p.add_argument(
        "--max_samples",
        type=int,
        default=None,
        help="最多使用前 N 条样本（默认 None 使用全部）",
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
        help="开启梯度检查点以省显存",
    )
    return p.parse_args()


def load_and_preprocess_data(csv_path: str, max_samples: Optional[int]) -> pd.DataFrame:
    print("正在加载数据...")
    df = pd.read_csv(csv_path)
    if max_samples is not None:
        df = df.iloc[:max_samples]
        print(f"使用样本上限: max_samples={max_samples}")

    df = df[df["Lsa_summary"].notna() & df["risk_deepseek"].notna()]
    df = df[df["risk_deepseek"] != 0]

    print(f"有效数据数量: {len(df)}")
    print(f"风险分布:\n{df['risk_deepseek'].value_counts().sort_index()}")
    return df


def create_prompt_template(text, risk_score, stock_symbol="STOCK") -> str:
    """与推理脚本一致的对话格式（few-shot 可按数据域替换）。"""
    system_prompt = (
        "You are a financial expert specializing in risk assessment for stock recommendations. "
        "Given a news summary, output a single risk score from 1 to 5 "
        "(1=very low, 2=low, 3=moderate when unclear, 4=high, 5=very high). "
        "Respond only with the score in the Assistant line as in the examples."
    )
    user_content = f"News to Stock Symbol -- {stock_symbol}: {text}"
    conversation = f"""System: {system_prompt}

User: News to Stock Symbol -- AAPL: Apple (AAPL) increases 22%
Assistant: 3

User: News to Stock Symbol -- AAPL: Apple (AAPL) price decreased 30%
Assistant: 4

User: News to Stock Symbol -- AAPL: Apple (AAPL) announced iPhone 15
Assistant: 3

User: {user_content}
Assistant: {risk_score}"""
    return conversation


def _labels_from_offsets(
    input_ids: List[int],
    offsets: List[tuple],
    answer_char_start: int,
) -> List[int]:
    labels: List[int] = []
    for i, tid in enumerate(input_ids):
        start, end = offsets[i]
        if start == 0 and end == 0:
            labels.append(-100)
        elif end <= answer_char_start:
            labels.append(-100)
        elif start >= answer_char_start:
            labels.append(tid)
        else:
            labels.append(tid)
    return labels


def _tokenize_one(
    tokenizer: PreTrainedTokenizerBase,
    text: str,
    max_length: int,
) -> Dict[str, Any]:
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
        risk = int(row["risk_deepseek"])
        stock_symbol = row.get("Stock_symbol", "STOCK")
        if pd.isna(raw) or raw == "":
            continue
        texts.append(create_prompt_template(raw, risk, stock_symbol))
        cls_labels.append(risk)

    stratify = cls_labels if len(set(cls_labels)) > 1 else None
    if stratify is not None:
        counts = pd.Series(cls_labels).value_counts()
        if counts.min() < 2:
            stratify = None
            print("警告: 某风险类别样本 < 2，无法 stratify，改用随机划分")

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


def create_model_and_tokenizer(model_path: str, gradient_checkpointing: bool):
    print(f"正在加载模型和分词器: {model_path}")

    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
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
