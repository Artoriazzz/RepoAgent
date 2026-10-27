#!/usr/bin/env python3
"""独立查询提取及向量召回入口：不接收按题号指定的经验清单。"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

from repo_agent.core.memory import RecalledMemory


def load_query_module():
    spec = importlib.util.spec_from_file_location(
        'vector_query', Path(__file__).with_name('memory_query.py'))
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rank_records(records, query_vectors, historical_vectors, repository):
    """候选只按经验类型与仓库过滤，题号和来源标签不参与打分。"""
    rankings = {}
    for kind in ('global_experience', 'repository_experience'):
        candidates = [r for r in records if r['memory_type'] == kind and
                      (kind == 'global_experience' or r['repository'] == repository)]
        rankings[kind] = sorted([
            {**r, 'score': max(sum(a * b for a, b in zip(q, d))
                              for q in query_vectors for d in historical_vectors[r['vector_key']])}
            for r in candidates
        ], key=lambda r: (-r['score'], r['memory_filename']))
    return rankings


def main():
    from repo_agent import cli
    from repo_agent.core import coordinator
    from repo_agent.core.experience import augment_coordinator_with_experiences
    from repo_agent.core.llm import ChatLLM
    from repo_agent.core.semantic_evidence import extract_public_problem_statement

    args = cli.build_parser().parse_args()
    if args.command != 'run' or not args.coordinator or args.repair_mode != 'benchmark' or args.no_repair_memory:
        raise SystemExit('Vector diagnostic requires benchmark Coordinator with memory enabled')
    config = json.loads(Path(os.environ['REPO_AGENT_VECTOR_CONFIG']).read_text())
    corpus = Path(config['corpus'])
    records = json.loads((corpus / 'records.json').read_text())
    encoded = json.loads((corpus / 'embeddings.json').read_text())
    query_type = load_query_module().MemoryQueryAgent

    def vector_augment(*, task, memory_store, limit_per_type=2):
        output = args.output_dir
        output.mkdir(parents=True, exist_ok=True)
        query = query_type(ChatLLM(
            model=config['model'], api_key=os.getenv('REPO_AGENT_MEMORY_QUERY_API_KEY') or os.getenv('OPENAI_API_KEY'),
            base_url=os.getenv('REPO_AGENT_MEMORY_QUERY_BASE_URL') or os.getenv('OPENAI_BASE_URL'),
            enable_thinking=False, reasoning_effort='low', max_output_tokens=4096,
            output_limit_recovery=False,
        )).extract(issue_body=extract_public_problem_statement(task), output_dir=output / 'memory-query')
        # API 错误不能伪装成成功召回，也不自动退回题号匹配或关键词检索。
        if query.status != 'success':
            raise RuntimeError(f'Vector query extraction failed: {query.status}')
        input_path = output / 'query_embedding_input.json'
        input_path.write_text(json.dumps([{'id': 'query', 'text': query.retrieval_text, 'is_query': True}], ensure_ascii=False))
        vectors_path = output / 'query_embeddings.json'
        subprocess.run([config['embedding_python'], str(Path(__file__).with_name('embed_retrieval_texts.py')),
                        '--input', str(input_path), '--output', str(vectors_path),
                        '--model-path', config['embedding_model_path']], check=True)
        vectors = json.loads(vectors_path.read_text())
        rankings = rank_records(records, vectors['vectors']['query'], encoded['vectors'], args.memory_scope)
        headers = {h.filename: h for h in memory_store.list_memories()}

        class VectorStore:
            def recall_experiences(self, query, *, memory_type, limit):
                return [RecalledMemory(header=headers[r['memory_filename']],
                                       content=memory_store.read_memory(r['memory_filename']))
                        for r in rankings[memory_type.value][:limit]]

        text, selected = augment_coordinator_with_experiences(task=task, memory_store=VectorStore(), limit_per_type=1)
        (output / 'vector_coordinator_input.md').write_text(text)
        (output / 'memory_retrieval.json').write_text(json.dumps({
            'query_status': query.status, 'selection': 'online_vector_top1_per_type',
            'repository_filter': args.memory_scope, 'rankings': rankings,
            'memories': [{'filename': m.header.filename, 'type': m.header.type.value,
                          'injected_content_sha256': hashlib.sha256(m.content.encode()).hexdigest()}
                         for m in selected],
            'coordinator_input_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'injection_clipped': '[... clipped ' in text,
        }, ensure_ascii=False, indent=2))
        return text, selected

    coordinator.augment_coordinator_with_experiences = vector_augment
    cli.main()


if __name__ == '__main__':
    main()
