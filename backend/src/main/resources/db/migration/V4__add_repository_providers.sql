ALTER TABLE code_repository
    MODIFY COLUMN github_repository_id BIGINT NULL,
    ADD COLUMN repository_provider VARCHAR(32) NOT NULL DEFAULT 'GITHUB',
    ADD COLUMN external_id VARCHAR(255),
    ADD COLUMN mirror_key VARCHAR(255);

UPDATE code_repository
SET external_id = CONCAT(owner_login, '/', name)
WHERE external_id IS NULL;

ALTER TABLE code_repository
    ADD CONSTRAINT uq_repository_provider_external_id
        UNIQUE (repository_provider, external_id);

ALTER TABLE repository_issue
    MODIFY COLUMN github_issue_number INTEGER NULL,
    ADD COLUMN external_id VARCHAR(255);

ALTER TABLE repository_issue
    ADD CONSTRAINT uq_repository_issue_external_id
        UNIQUE (repository_id, external_id);
