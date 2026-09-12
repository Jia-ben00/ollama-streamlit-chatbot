"""BiLSTM 情感分类模型定义。

使用双向 LSTM + 全连接层进行二分类（正面/负面）。
"""

import torch
import torch.nn as nn

from sentiment_analysis.config import config


class BiLSTMSentiment(nn.Module):
    """双向 LSTM 文本情感分类模型。

    架构:
        Embedding -> BiLSTM -> Dropout -> Linear -> Sigmoid/Softmax
    """

    def __init__(
        self,
        vocab_size: int,
        embedding_dim: int = config.embedding_dim,
        hidden_dim: int = config.hidden_dim,
        num_layers: int = config.num_layers,
        bidirectional: bool = config.bidirectional,
        dropout: float = config.dropout,
        num_classes: int = config.num_classes,
        pad_idx: int = 0,
    ):
        super().__init__()

        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.bidirectional = bidirectional
        self.num_directions = 2 if bidirectional else 1

        # 词嵌入层
        self.embedding = nn.Embedding(
            num_embeddings=vocab_size,
            embedding_dim=embedding_dim,
            padding_idx=pad_idx,
        )

        # 双向 LSTM
        self.lstm = nn.LSTM(
            input_size=embedding_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            bidirectional=bidirectional,
            dropout=dropout if num_layers > 1 else 0.0,
            batch_first=True,
        )

        # Dropout
        self.dropout = nn.Dropout(dropout)

        # 全连接分类层
        # 双向 LSTM 输出维度为 hidden_dim * 2
        fc_input_dim = hidden_dim * self.num_directions
        self.fc = nn.Linear(fc_input_dim, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """前向传播。

        Args:
            x: 输入张量，形状 (batch_size, seq_length)，值为词索引。

        Returns:
            输出 logits，形状 (batch_size, num_classes)。
        """
        # x: (batch, seq_len)
        embedded = self.embedding(x)  # (batch, seq_len, embedding_dim)

        # LSTM 前向传播
        # output: (batch, seq_len, hidden_dim * num_directions)
        # (h_n, c_n): 最终隐藏状态和细胞状态
        lstm_out, (h_n, c_n) = self.lstm(embedded)

        # 取最后一个时间步的输出（对于双向，取正向最后一步和反向第一步的拼接）
        # h_n shape: (num_layers * num_directions, batch, hidden_dim)
        # 取最后一层的正向和反向隐藏状态
        if self.bidirectional:
            # 最后一层的正向隐藏状态
            h_forward = h_n[-2, :, :]   # (batch, hidden_dim)
            # 最后一层的反向隐藏状态
            h_backward = h_n[-1, :, :]  # (batch, hidden_dim)
            # 拼接
            final_hidden = torch.cat((h_forward, h_backward), dim=1)  # (batch, hidden_dim * 2)
        else:
            final_hidden = h_n[-1, :, :]  # (batch, hidden_dim)

        # Dropout
        dropped = self.dropout(final_hidden)

        # 全连接层
        logits = self.fc(dropped)  # (batch, num_classes)

        return logits

    def predict_proba(self, x: torch.Tensor) -> torch.Tensor:
        """预测概率分布。"""
        logits = self.forward(x)
        return torch.softmax(logits, dim=1)

    def predict(self, x: torch.Tensor) -> torch.Tensor:
        """预测类别标签。"""
        logits = self.forward(x)
        return torch.argmax(logits, dim=1)


def count_parameters(model: nn.Module) -> int:
    """统计模型可训练参数数量。"""
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
