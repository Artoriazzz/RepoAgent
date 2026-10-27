package com.artoria.repoagentbackend.memory;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;
import java.util.UUID;

public interface MemoryObservationJpaRepository extends JpaRepository<MemoryObservation, UUID> {
    Optional<MemoryObservation> findByIdempotencyKey(String idempotencyKey);
}
