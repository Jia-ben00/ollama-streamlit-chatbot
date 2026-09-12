"""中文情感分析模型训练脚本。

使用字符级分词的 BiLSTM 模型训练中文情感分类器。
"""

import os
import sys
import time
import json

import torch
import torch.nn as nn
import torch.optim as optim

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentiment_analysis.config import config
from sentiment_analysis.dataset_chinese import create_chinese_dataloaders
from sentiment_analysis.model import BiLSTMSentiment, count_parameters


# 中文模型专用配置
CHINESE_CONFIG = {
    "batch_size": 32,
    "max_seq_length": 150,
    "max_vocab_size": 3000,
    "embedding_dim": 64,
    "hidden_dim": 64,
    "num_layers": 2,
    "bidirectional": True,
    "dropout": 0.3,
    "num_classes": 2,
    "learning_rate": 1e-3,
    "num_epochs": 15,
    "weight_decay": 1e-5,
    "grad_clip": 5.0,
    "model_name": "bilstm_chinese_sentiment.pt",
    "vocab_name": "vocab_chinese.json",
}


def set_seed(seed: int):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    import random
    import numpy as np
    random.seed(seed)
    np.random.seed(seed)


def get_device() -> torch.device:
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"使用 GPU: {torch.cuda.get_device_name(0)}")
    else:
        device = torch.device("cpu")
        print("使用 CPU")
    return device


def train_epoch(model, loader, optimizer, criterion, device, epoch):
    model.train()
    total_loss = 0.0
    correct = 0
    total = 0
    for batch_idx, (texts, labels) in enumerate(loader):
        texts, labels = texts.to(device), labels.to(device)
        optimizer.zero_grad()
        logits = model(texts)
        loss = criterion(logits, labels)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), CHINESE_CONFIG["grad_clip"])
        optimizer.step()
        total_loss += loss.item() * texts.size(0)
        preds = torch.argmax(logits, dim=1)
        correct += (preds == labels).sum().item()
        total += texts.size(0)
    return total_loss / total, correct / total


@torch.no_grad()
def evaluate(model, loader, criterion, device):
    model.eval()
    total_loss = 0.0
    correct = 0
    total = 0
    for texts, labels in loader:
        texts, labels = texts.to(device), labels.to(device)
        logits = model(texts)
        loss = criterion(logits, labels)
        total_loss += loss.item() * texts.size(0)
        preds = torch.argmax(logits, dim=1)
        correct += (preds == labels).sum().item()
        total += texts.size(0)
    return total_loss / total, correct / total


def main():
    print("=" * 60)
    print("  中文文本情感分析 - BiLSTM 训练")
    print("=" * 60)

    set_seed(config.random_seed)
    device = get_device()

    print("\n[1/4] 加载中文数据...")
    train_loader, test_loader, vocab = create_chinese_dataloaders(
        batch_size=CHINESE_CONFIG["batch_size"],
        max_length=CHINESE_CONFIG["max_seq_length"],
        max_vocab_size=CHINESE_CONFIG["max_vocab_size"],
    )

    print("\n[2/4] 构建模型...")
    model = BiLSTMSentiment(
        vocab_size=len(vocab),
        embedding_dim=CHINESE_CONFIG["embedding_dim"],
        hidden_dim=CHINESE_CONFIG["hidden_dim"],
        num_layers=CHINESE_CONFIG["num_layers"],
        bidirectional=CHINESE_CONFIG["bidirectional"],
        dropout=CHINESE_CONFIG["dropout"],
        num_classes=CHINESE_CONFIG["num_classes"],
        pad_idx=vocab.char2idx[vocab.PAD_TOKEN],
    ).to(device)
    print(f"模型参数量: {count_parameters(model):,}")

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(
        model.parameters(),
        lr=CHINESE_CONFIG["learning_rate"],
        weight_decay=CHINESE_CONFIG["weight_decay"],
    )

    num_epochs = CHINESE_CONFIG["num_epochs"]
    print(f"\n[3/4] 开始训练（共 {num_epochs} 个 epoch）...")
    best_acc = 0.0
    os.makedirs(config.checkpoint_dir, exist_ok=True)

    for epoch in range(num_epochs):
        start = time.time()
        train_loss, train_acc = train_epoch(model, train_loader, optimizer, criterion, device, epoch)
        test_loss, test_acc = evaluate(model, test_loader, criterion, device)
        elapsed = time.time() - start

        print(f"Epoch {epoch+1}/{num_epochs} ({elapsed:.1f}s) "
              f"| 训练 Loss: {train_loss:.4f} Acc: {train_acc:.4f} "
              f"| 测试 Loss: {test_loss:.4f} Acc: {test_acc:.4f}")

        if test_acc > best_acc:
            best_acc = test_acc
            model_path = os.path.join(config.checkpoint_dir, CHINESE_CONFIG["model_name"])
            torch.save({
                "epoch": epoch + 1,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "test_acc": test_acc,
                "vocab_size": len(vocab),
                "config": {
                    "embedding_dim": CHINESE_CONFIG["embedding_dim"],
                    "hidden_dim": CHINESE_CONFIG["hidden_dim"],
                    "num_layers": CHINESE_CONFIG["num_layers"],
                    "bidirectional": CHINESE_CONFIG["bidirectional"],
                    "dropout": CHINESE_CONFIG["dropout"],
                    "num_classes": CHINESE_CONFIG["num_classes"],
                },
                "language": "chinese",
            }, model_path)
            print(f"  ★ 保存最佳中文模型 (Acc: {test_acc:.4f})")

    # 保存中文词汇表
    vocab_path = os.path.join(config.checkpoint_dir, CHINESE_CONFIG["vocab_name"])
    vocab.save(vocab_path)
    print(f"\n中文词汇表已保存 -> {vocab_path}")

    print("\n" + "=" * 60)
    print(f"中文模型训练完成！最佳测试集准确率: {best_acc:.4f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
