ALTER TABLE semantic_memory
    ADD COLUMN retrieval_text TEXT NULL AFTER summary,
    ADD COLUMN embedding_profile VARCHAR(128) NULL AFTER embedding_model,
    ADD COLUMN embedding_dimensions INTEGER NULL AFTER embedding_profile;

UPDATE semantic_memory
SET retrieval_text = summary
WHERE retrieval_text IS NULL OR TRIM(retrieval_text) = '';

UPDATE semantic_memory
SET embedding_profile = 'legacy-v0'
WHERE embedding_json IS NOT NULL AND embedding_profile IS NULL;

ALTER TABLE semantic_memory
    MODIFY COLUMN retrieval_text TEXT NOT NULL;

ALTER TABLE memory_revision
    ADD COLUMN retrieval_text TEXT NULL AFTER summary;

UPDATE memory_revision
SET retrieval_text = summary
WHERE retrieval_text IS NULL OR TRIM(retrieval_text) = '';

ALTER TABLE memory_revision
    MODIFY COLUMN retrieval_text TEXT NOT NULL;

ALTER TABLE memory_index_job
    ADD COLUMN embedding_profile VARCHAR(128) NOT NULL DEFAULT 'legacy-v0' AFTER memory_revision;

ALTER TABLE memory_index_job
    DROP INDEX uk_memory_index_revision,
    ADD CONSTRAINT uk_memory_index_revision_profile
        UNIQUE (memory_id, memory_revision, embedding_profile);
