"""模型评估脚本。

在测试集上评估训练好的模型，输出准确率、精确率、召回率、F1 分数和混淆矩阵。
"""

import os
import sys

import torch
import torch.nn as nn
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentiment_analysis.config import config
from sentiment_analysis.dataset import create_dataloaders, Vocabulary
from sentiment_analysis.model import BiLSTMSentiment


def load_model(checkpoint_path: str, vocab: Vocabulary, device: torch.device) -> BiLSTMSentiment:
    """从检查点加载模型。"""
    checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
    model_config = checkpoint["config"]

    model = BiLSTMSentiment(
        vocab_size=checkpoint["vocab_size"],
        embedding_dim=model_config["embedding_dim"],
        hidden_dim=model_config["hidden_dim"],
        num_layers=model_config["num_layers"],
        bidirectional=model_config["bidirectional"],
        dropout=model_config["dropout"],
        num_classes=model_config["num_classes"],
        pad_idx=vocab.word2idx[vocab.PAD_TOKEN],
    ).to(device)

    model.load_state_dict(checkpoint["model_state_dict"])
    return model


def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray, num_classes: int = 2):
    """计算分类指标：准确率、精确率、召回率、F1、混淆矩阵。"""
    accuracy = np.mean(y_true == y_pred)

    # 混淆矩阵
    confusion = np.zeros((num_classes, num_classes), dtype=int)
    for t, p in zip(y_true, y_pred):
        confusion[t][p] += 1

    # 每个类别的精确率、召回率、F1
    precision = []
    recall = []
    f1 = []
    for i in range(num_classes):
        tp = confusion[i][i]
        fp = sum(confusion[j][i] for j in range(num_classes) if j != i)
        fn = sum(confusion[i][j] for j in range(num_classes) if j != i)

        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0

        precision.append(prec)
        recall.append(rec)
        f1.append(f)

    # 宏平均
    macro_precision = np.mean(precision)
    macro_recall = np.mean(recall)
    macro_f1 = np.mean(f1)

    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "macro_precision": macro_precision,
        "macro_recall": macro_recall,
        "macro_f1": macro_f1,
        "confusion_matrix": confusion,
    }


def print_metrics(metrics: dict, class_names: list = None):
    """打印评估指标。"""
    if class_names is None:
        class_names = ["负面 (Negative)", "正面 (Positive)"]

    print("\n" + "=" * 50)
    print("  模型评估结果")
    print("=" * 50)
    print(f"  准确率 (Accuracy):  {metrics['accuracy']:.4f}")
    print(f"  宏平均精确率:        {metrics['macro_precision']:.4f}")
    print(f"  宏平均召回率:        {metrics['macro_recall']:.4f}")
    print(f"  宏平均 F1 分数:      {metrics['macro_f1']:.4f}")

    print("\n  各类别指标:")
    print(f"  {'类别':<20} {'精确率':>8} {'召回率':>8} {'F1':>8}")
    print("  " + "-" * 48)
    for i, name in enumerate(class_names):
        print(f"  {name:<20} {metrics['precision'][i]:>8.4f} "
              f"{metrics['recall'][i]:>8.4f} {metrics['f1'][i]:>8.4f}")

    print("\n  混淆矩阵:")
    print(f"  {'':>12} {'预测负面':>10} {'预测正面':>10}")
    print(f"  {'真实负面':>12} {metrics['confusion_matrix'][0][0]:>10} {metrics['confusion_matrix'][0][1]:>10}")
    print(f"  {'真实正面':>12} {metrics['confusion_matrix'][1][0]:>10} {metrics['confusion_matrix'][1][1]:>10}")
    print("=" * 50)


def main():
    """主评估函数。"""
    print("=" * 60)
    print("  基于 PyTorch 的文本情感分析 - 模型评估")
    print("=" * 60)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"使用设备: {device}")

    # 加载数据（使用相同的词汇表）
    checkpoint_path = os.path.join(config.checkpoint_dir, config.model_name)
    vocab_path = os.path.join(config.checkpoint_dir, config.vocab_name)

    if not os.path.exists(checkpoint_path):
        print(f"错误：未找到模型检查点 {checkpoint_path}")
        print("请先运行 train.py 训练模型。")
        return

    # 加载词汇表
    vocab = Vocabulary.load(vocab_path)
    print(f"词汇表大小: {len(vocab)}")

    # 加载数据
    _, test_loader, _ = create_dataloaders(
        batch_size=config.batch_size,
        max_length=config.max_seq_length,
        max_vocab_size=config.max_vocab_size,
    )

    # 加载模型
    model = load_model(checkpoint_path, vocab, device)
    model.eval()
    print("模型加载成功")

    # 评估
    all_preds = []
    all_labels = []

    with torch.no_grad():
        for texts, labels in test_loader:
            texts = texts.to(device)
            logits = model(texts)
            preds = torch.argmax(logits, dim=1)
            all_preds.extend(preds.cpu().numpy())
            all_labels.extend(labels.numpy())

    y_true = np.array(all_labels)
    y_pred = np.array(all_preds)

    # 计算并打印指标
    metrics = compute_metrics(y_true, y_pred)
    print_metrics(metrics)


if __name__ == "__main__":
    main()
