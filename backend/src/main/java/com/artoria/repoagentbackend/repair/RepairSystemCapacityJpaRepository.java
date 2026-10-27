package com.artoria.repoagentbackend.repair;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import java.util.Optional;
public interface RepairSystemCapacityJpaRepository extends JpaRepository<RepairSystemCapacity, String> {
    @Lock(LockModeType.PESSIMISTIC_WRITE) @Query("select value from RepairSystemCapacity value where value.capacityKey = :key")
    Optional<RepairSystemCapacity> findByCapacityKeyForUpdate(@Param("key") String key);
}
