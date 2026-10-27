ALTER TABLE semantic_memory
    MODIFY COLUMN quality_score DOUBLE NOT NULL DEFAULT 0.5;

ALTER TABLE memory_recall
    MODIFY COLUMN recall_score DOUBLE NOT NULL;
