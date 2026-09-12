"""模型训练脚本。

完整训练流程：数据加载 -> 模型构建 -> 训练循环 -> 验证 -> 保存模型。
"""

import os
import sys
import time
import json

import torch
import torch.nn as nn
import torch.optim as optim

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentiment_analysis.config import config
from sentiment_analysis.dataset import create_dataloaders
from sentiment_analysis.model import BiLSTMSentiment, count_parameters


def set_seed(seed: int):
    """设置随机种子以保证可复现。"""
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    import random
    import numpy as np
    random.seed(seed)
    np.random.seed(seed)
    if torch.cuda.is_available():
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


def get_device() -> torch.device:
    """自动选择可用设备（GPU 优先）。"""
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"使用 GPU: {torch.cuda.get_device_name(0)}")
        print(f"GPU 显存: {torch.cuda.get_device_properties(0).total_mem / 1024**3:.1f} GB")
    else:
        device = torch.device("cpu")
        print("使用 CPU（未检测到 CUDA）")
    return device


def train_epoch(
    model: nn.Module,
    loader: torch.utils.data.DataLoader,
    optimizer: optim.Optimizer,
    criterion: nn.Module,
    device: torch.device,
    epoch: int,
) -> Tuple[float, float]:
    """训练一个 epoch。

    Returns:
        (平均损失, 准确率)
    """
    model.train()
    total_loss = 0.0
    correct = 0
    total = 0

    for batch_idx, (texts, labels) in enumerate(loader):
        texts = texts.to(device)
        labels = labels.to(device)

        optimizer.zero_grad()
        logits = model(texts)
        loss = criterion(logits, labels)
        loss.backward()

        # 梯度裁剪
        torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)

        optimizer.step()

        total_loss += loss.item() * texts.size(0)
        preds = torch.argmax(logits, dim=1)
        correct += (preds == labels).sum().item()
        total += texts.size(0)

        if (batch_idx + 1) % config.log_interval == 0:
            print(f"  Epoch {epoch+1} [{batch_idx+1}/{len(loader)}] "
                  f"Loss: {loss.item():.4f} Acc: {correct/total:.4f}")

    avg_loss = total_loss / total
    accuracy = correct / total
    return avg_loss, accuracy


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: torch.utils.data.DataLoader,
    criterion: nn.Module,
    device: torch.device,
) -> Tuple[float, float]:
    """在验证/测试集上评估模型。

    Returns:
        (平均损失, 准确率)
    """
    model.eval()
    total_loss = 0.0
    correct = 0
    total = 0

    for texts, labels in loader:
        texts = texts.to(device)
        labels = labels.to(device)

        logits = model(texts)
        loss = criterion(logits, labels)

        total_loss += loss.item() * texts.size(0)
        preds = torch.argmax(logits, dim=1)
        correct += (preds == labels).sum().item()
        total += texts.size(0)

    avg_loss = total_loss / total
    accuracy = correct / total
    return avg_loss, accuracy


def main():
    """主训练函数。"""
    print("=" * 60)
    print("  基于 PyTorch 的文本情感分析 - 训练脚本")
    print("=" * 60)

    # 设置随机种子
    set_seed(config.random_seed)

    # 选择设备
    device = get_device()

    # 加载数据
    print("\n[1/4] 加载数据...")
    train_loader, test_loader, vocab = create_dataloaders(
        batch_size=config.batch_size,
        max_length=config.max_seq_length,
        max_vocab_size=config.max_vocab_size,
    )

    # 构建模型
    print("\n[2/4] 构建模型...")
    model = BiLSTMSentiment(
        vocab_size=len(vocab),
        embedding_dim=config.embedding_dim,
        hidden_dim=config.hidden_dim,
        num_layers=config.num_layers,
        bidirectional=config.bidirectional,
        dropout=config.dropout,
        num_classes=config.num_classes,
        pad_idx=vocab.word2idx[vocab.PAD_TOKEN],
    ).to(device)

    print(f"模型参数量: {count_parameters(model):,}")

    # 定义损失函数和优化器
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )

    # 训练循环
    print(f"\n[3/4] 开始训练（共 {config.num_epochs} 个 epoch）...")
    best_acc = 0.0
    history = {"train_loss": [], "train_acc": [], "test_loss": [], "test_acc": []}

    os.makedirs(config.checkpoint_dir, exist_ok=True)

    for epoch in range(config.num_epochs):
        start_time = time.time()

        train_loss, train_acc = train_epoch(
            model, train_loader, optimizer, criterion, device, epoch
        )
        test_loss, test_acc = evaluate(model, test_loader, criterion, device)

        elapsed = time.time() - start_time

        # 记录历史
        history["train_loss"].append(train_loss)
        history["train_acc"].append(train_acc)
        history["test_loss"].append(test_loss)
        history["test_acc"].append(test_acc)

        print(f"\nEpoch {epoch+1}/{config.num_epochs} ({elapsed:.1f}s)")
        print(f"  训练集 - Loss: {train_loss:.4f}, Acc: {train_acc:.4f}")
        print(f"  测试集 - Loss: {test_loss:.4f}, Acc: {test_acc:.4f}")

        # 保存最佳模型
        if test_acc > best_acc:
            best_acc = test_acc
            model_path = os.path.join(config.checkpoint_dir, config.model_name)
            torch.save({
                "epoch": epoch + 1,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "test_acc": test_acc,
                "vocab_size": len(vocab),
                "config": {
                    "embedding_dim": config.embedding_dim,
                    "hidden_dim": config.hidden_dim,
                    "num_layers": config.num_layers,
                    "bidirectional": config.bidirectional,
                    "dropout": config.dropout,
                    "num_classes": config.num_classes,
                },
            }, model_path)
            print(f"  ★ 保存最佳模型 (Acc: {test_acc:.4f}) -> {model_path}")

    # 保存词汇表
    vocab_path = os.path.join(config.checkpoint_dir, config.vocab_name)
    vocab.save(vocab_path)
    print(f"\n词汇表已保存 -> {vocab_path}")

    # 保存训练历史
    history_path = os.path.join(config.checkpoint_dir, "training_history.json")
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    print(f"训练历史已保存 -> {history_path}")

    # 最终结果
    print("\n" + "=" * 60)
    print(f"训练完成！最佳测试集准确率: {best_acc:.4f}")
    print("=" * 60)


# 修复类型提示（Tuple 需要导入）
from typing import Tuple

if __name__ == "__main__":
    main()
