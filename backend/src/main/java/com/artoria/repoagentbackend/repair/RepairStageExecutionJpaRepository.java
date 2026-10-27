package com.artoria.repoagentbackend.repair;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
public interface RepairStageExecutionJpaRepository extends JpaRepository<RepairStageExecution, UUID> {
    @Lock(LockModeType.PESSIMISTIC_WRITE) @Query("select value from RepairStageExecution value where value.taskId = :taskId and value.stageSequence = :sequence")
    Optional<RepairStageExecution> findForUpdate(@Param("taskId") UUID taskId, @Param("sequence") long sequence);
    List<RepairStageExecution> findByTaskIdOrderByStageSequenceAsc(UUID taskId);
}
