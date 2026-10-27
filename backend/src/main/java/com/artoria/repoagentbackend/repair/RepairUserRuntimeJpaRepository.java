package com.artoria.repoagentbackend.repair;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import java.util.Optional;
import java.util.UUID;
public interface RepairUserRuntimeJpaRepository extends JpaRepository<RepairUserRuntime, UUID> {
    @Lock(LockModeType.PESSIMISTIC_WRITE) @Query("select value from RepairUserRuntime value where value.userId = :userId")
    Optional<RepairUserRuntime> findByUserIdForUpdate(@Param("userId") UUID userId);
}
