package com.artoria.repoagentbackend.repair;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import java.time.LocalDate;
import java.util.Optional;
import java.util.UUID;
public interface RepairDailyUsageJpaRepository extends JpaRepository<RepairDailyUsage, UUID> {
    @Lock(LockModeType.PESSIMISTIC_WRITE) @Query("select value from RepairDailyUsage value where value.userId = :userId and value.usageDate = :date")
    Optional<RepairDailyUsage> findForUpdate(@Param("userId") UUID userId, @Param("date") LocalDate date);
    Optional<RepairDailyUsage> findByUserIdAndUsageDate(UUID userId, LocalDate date);
}
