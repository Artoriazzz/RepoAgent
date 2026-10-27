"""在已有 embedding 环境中离线编码检索文本，返回归一化的分块向量。"""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-path", required=True, type=Path)
    args = parser.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(2)
    model = SentenceTransformer(str(args.model_path), device="cpu", local_files_only=True)
    inputs = json.loads(args.input.read_text())
    instruction = "为这个句子生成表示以用于检索相关文章："
    texts, spans, counts = [], {}, {}
    for item in inputs:
        prefix = instruction if item["is_query"] else ""
        ids = model.tokenizer.encode(item["text"], add_special_tokens=False)
        prefix_ids = model.tokenizer.encode(prefix, add_special_tokens=False)
        size = model.max_seq_length - model.tokenizer.num_special_tokens_to_add() - len(prefix_ids)
        if size <= 0 or not ids:
            raise ValueError("Invalid embedding text or context limit")
        chunks = [item["text"]] if len(ids) <= size else [
            model.tokenizer.decode(ids[start:start + size], skip_special_tokens=True,
                                   clean_up_tokenization_spaces=False)
            for start in range(0, len(ids), size)
        ]
        begin = len(texts)
        texts.extend(prefix + chunk for chunk in chunks)
        spans[item["id"]] = (begin, len(texts))
        counts[item["id"]] = {"tokens_without_instruction": len(ids), "chunks": len(chunks)}
    # 显式分块并校验，避免 encode 默认截断丢掉长查询后半部分。
    if any(len(model.tokenizer.encode(text)) > model.max_seq_length for text in texts):
        raise ValueError("A prepared chunk still exceeds the embedding context limit")
    vectors = model.encode(texts, batch_size=8, normalize_embeddings=True,
                           convert_to_numpy=True, show_progress_bar=False)
    result = {
        "model": "BAAI/bge-small-zh-v1.5", "model_path": str(args.model_path),
        "dimensions": int(vectors.shape[1]), "max_seq_length": model.max_seq_length,
        "normalization": "l2", "pooling": "model_config",
        "query_instruction": instruction, "counts": counts,
        "encoded_chunks": len(texts),
        "vectors": {key: vectors[begin:end].tolist() for key, (begin, end) in spans.items()},
        "encoded_texts": {key: texts[begin:end] for key, (begin, end) in spans.items()},
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps({"model": result["model"], "dimensions": result["dimensions"],
                      "unique_texts": len(inputs), "encoded_chunks": len(texts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
