"""单条文本情感推理脚本。

加载训练好的模型，对输入文本进行情感分类预测。
"""

import os
import sys

import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentiment_analysis.config import config
from sentiment_analysis.dataset import Vocabulary
from sentiment_analysis.dataset_chinese import ChineseVocabulary
from sentiment_analysis.model import BiLSTMSentiment


CHINESE_MODEL_NAME = "bilstm_chinese_sentiment.pt"
CHINESE_VOCAB_NAME = "vocab_chinese.json"
CHINESE_MAX_SEQ_LENGTH = 150


class SentimentPredictor:
    """情感分析预测器，封装模型加载和推理。"""

    def __init__(
        self,
        checkpoint_path: str = None,
        vocab_path: str = None,
        device: torch.device = None,
    ):
        if checkpoint_path is None:
            checkpoint_path = os.path.join(config.checkpoint_dir, config.model_name)
        if vocab_path is None:
            vocab_path = os.path.join(config.checkpoint_dir, config.vocab_name)
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.device = device
        self.vocab = Vocabulary.load(vocab_path)

        # 加载模型
        checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
        model_config = checkpoint["config"]
        self.model = BiLSTMSentiment(
            vocab_size=checkpoint["vocab_size"],
            embedding_dim=model_config["embedding_dim"],
            hidden_dim=model_config["hidden_dim"],
            num_layers=model_config["num_layers"],
            bidirectional=model_config["bidirectional"],
            dropout=model_config["dropout"],
            num_classes=model_config["num_classes"],
            pad_idx=self.vocab.word2idx[self.vocab.PAD_TOKEN],
        ).to(device)
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()

        self.class_names = ["负面 (Negative)", "正面 (Positive)"]

    def predict(self, text: str) -> dict:
        """对单条文本进行情感预测。

        Args:
            text: 输入文本。

        Returns:
            包含预测标签、类别名和概率的字典。
        """
        encoded = self.vocab.encode(text, max_length=config.max_seq_length)
        input_tensor = torch.tensor([encoded], dtype=torch.long).to(self.device)

        with torch.no_grad():
            logits = self.model(input_tensor)
            probs = torch.softmax(logits, dim=1).cpu().numpy()[0]
            pred_label = int(torch.argmax(logits, dim=1).item())

        return {
            "text": text,
            "predicted_label": pred_label,
            "predicted_class": self.class_names[pred_label],
            "probabilities": {
                self.class_names[i]: float(probs[i])
                for i in range(len(self.class_names))
            },
            "confidence": float(max(probs)),
        }

    def predict_batch(self, texts: list) -> list:
        """批量预测。"""
        return [self.predict(text) for text in texts]


class ChineseSentimentPredictor:
    """中文情感分析预测器。"""

    def __init__(
        self,
        checkpoint_path: str = None,
        vocab_path: str = None,
        device: torch.device = None,
    ):
        if checkpoint_path is None:
            checkpoint_path = os.path.join(config.checkpoint_dir, CHINESE_MODEL_NAME)
        if vocab_path is None:
            vocab_path = os.path.join(config.checkpoint_dir, CHINESE_VOCAB_NAME)
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.device = device
        self.vocab = ChineseVocabulary.load(vocab_path)

        checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
        model_config = checkpoint["config"]
        self.model = BiLSTMSentiment(
            vocab_size=checkpoint["vocab_size"],
            embedding_dim=model_config["embedding_dim"],
            hidden_dim=model_config["hidden_dim"],
            num_layers=model_config["num_layers"],
            bidirectional=model_config["bidirectional"],
            dropout=model_config["dropout"],
            num_classes=model_config["num_classes"],
            pad_idx=self.vocab.char2idx[self.vocab.PAD_TOKEN],
        ).to(device)
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()

        self.class_names = ["负面 (Negative)", "正面 (Positive)"]

    def predict(self, text: str) -> dict:
        """对中文文本进行情感预测。"""
        encoded = self.vocab.encode(text, max_length=CHINESE_MAX_SEQ_LENGTH)
        input_tensor = torch.tensor([encoded], dtype=torch.long).to(self.device)

        with torch.no_grad():
            logits = self.model(input_tensor)
            probs = torch.softmax(logits, dim=1).cpu().numpy()[0]
            pred_label = int(torch.argmax(logits, dim=1).item())

        return {
            "text": text,
            "predicted_label": pred_label,
            "predicted_class": self.class_names[pred_label],
            "probabilities": {
                self.class_names[i]: float(probs[i])
                for i in range(len(self.class_names))
            },
            "confidence": float(max(probs)),
        }

    def predict_batch(self, texts: list) -> list:
        return [self.predict(text) for text in texts]


def main():
    """交互式推理演示。"""
    print("=" * 60)
    print("  文本情感分析 - 推理演示")
    print("=" * 60)

    try:
        predictor = SentimentPredictor()
    except FileNotFoundError as e:
        print(f"错误：{e}")
        print("请先运行 train.py 训练模型。")
        return

    print(f"模型加载成功（设备: {predictor.device}）")
    print("\n输入文本进行情感分析，输入 'quit' 退出。\n")

    # 演示示例
    demo_texts = [
        "This movie is absolutely wonderful and I loved every minute of it",
        "This movie is terrible and I hated every minute of it",
        "The acting was superb but the story was a bit slow",
        "I can not believe I wasted two hours of my life on this",
    ]
    print("--- 演示示例 ---")
    for text in demo_texts:
        result = predictor.predict(text)
        print(f"\n文本: {text}")
        print(f"预测: {result['predicted_class']} (置信度: {result['confidence']:.2%})")
        for cls, prob in result["probabilities"].items():
            print(f"  {cls}: {prob:.2%}")

    print("\n--- 交互式输入 ---")
    while True:
        text = input("\n请输入文本: ").strip()
        if text.lower() in ("quit", "exit", "q"):
            break
        if not text:
            continue
        result = predictor.predict(text)
        print(f"预测结果: {result['predicted_class']}")
        print(f"置信度: {result['confidence']:.2%}")
        for cls, prob in result["probabilities"].items():
            print(f"  {cls}: {prob:.2%}")


if __name__ == "__main__":
    main()
