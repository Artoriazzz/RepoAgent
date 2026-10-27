CREATE TABLE app_user (
    id CHAR(36) PRIMARY KEY,
    github_user_id BIGINT NOT NULL UNIQUE,
    login VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE code_repository
    ADD COLUMN owner_user_id CHAR(36),
    ADD CONSTRAINT fk_code_repository_owner FOREIGN KEY (owner_user_id) REFERENCES app_user(id);

CREATE TABLE repository_member (
    id CHAR(36) PRIMARY KEY,
    repository_id CHAR(36) NOT NULL,
    user_id CHAR(36) NOT NULL,
    member_role VARCHAR(32) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_repository_member_repository FOREIGN KEY (repository_id) REFERENCES code_repository(id),
    CONSTRAINT fk_repository_member_user FOREIGN KEY (user_id) REFERENCES app_user(id),
    CONSTRAINT uq_repository_member UNIQUE (repository_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE INDEX ix_repository_member_user_id ON repository_member(user_id);
CREATE INDEX ix_code_repository_owner_user_id ON code_repository(owner_user_id);
