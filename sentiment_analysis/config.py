"""情感分析模型超参数配置。"""

from dataclasses import dataclass


@dataclass
class SentimentConfig:
    """情感分析模型配置。"""

    # 数据参数
    max_vocab_size: int = 10000       # 词汇表最大大小
    max_seq_length: int = 200         # 句子最大长度
    min_word_freq: int = 1            # 最低词频

    # 模型参数
    embedding_dim: int = 128          # 词向量维度
    hidden_dim: int = 128             # LSTM 隐藏层维度
    num_layers: int = 2               # LSTM 层数
    bidirectional: bool = True        # 是否双向
    dropout: float = 0.3              # Dropout 比率
    num_classes: int = 2              # 分类数（正面/负面）

    # 训练参数
    batch_size: int = 64              # 批次大小
    learning_rate: float = 1e-3       # 学习率
    num_epochs: int = 10              # 训练轮数
    weight_decay: float = 1e-5        # 权重衰减
    grad_clip: float = 5.0            # 梯度裁剪阈值

    # 路径
    checkpoint_dir: str = "sentiment_analysis/checkpoints"
    data_dir: str = "sentiment_analysis/data"
    model_name: str = "bilstm_sentiment.pt"
    vocab_name: str = "vocab.json"

    # 其他
    random_seed: int = 42             # 随机种子
    log_interval: int = 10            # 日志打印间隔（batch 数）


# 全局配置实例
config = SentimentConfig()
