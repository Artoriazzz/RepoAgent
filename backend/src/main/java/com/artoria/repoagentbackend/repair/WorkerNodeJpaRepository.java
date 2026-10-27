package com.artoria.repoagentbackend.repair;

import org.springframework.data.jpa.repository.JpaRepository;

public interface WorkerNodeJpaRepository extends JpaRepository<WorkerNode, String> {
}
