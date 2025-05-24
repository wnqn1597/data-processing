# FastText 模型训练

## 运行

运行 `train.ipynb`

## 依赖

```
datasets==3.5.0
fasttext==0.9.2
```

## 数据处理

- 从openwebmath与fineweb数据集中各取50,000条样本，作为训练样本
- 从fineweb中取5,000条样本作为测试数据
- 转换为fasttext数据格式：
  `__label__{label} {text}`

## 训练

- 利用 `fasttext` 库训练FastText模型
- 参数设置：`epoch=10, lr=0.5, wordNgrams=2`

## 效果评估

- 在训练集上的指标：Accuracy=0.9892
- 经过fasttext打标后的5000条fineweb数据：`predict.txt`