package com.artoria.repoagentbackend.repair;

import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class RepairPipelineStageTest {

    @Test
    void movesOneTaskAcrossTheDurablePipelineAndCompletesOnlyAfterMemory() {
        RepairTask task = queuedTask();

        UUID coordinatorLease = task.claimPipelineStage(
                RepairPipelineStage.COORDINATOR, "coordinator-1", 90
        );
        task.handoffPipelineStage(
                RepairPipelineStage.COORDINATOR,
                RepairPipelineStage.IMPLEMENT,
                "coordinator-1",
                coordinatorLease,
                "brief ready"
        );
        assertThat(task.getPipelineStage()).isEqualTo(RepairPipelineStage.IMPLEMENT);
        assertThat(task.getStatus()).isEqualTo(RepairTaskStatus.QUEUED);
        assertThat(task.getWorkerId()).isNull();

        UUID implementLease = task.claimPipelineStage(
                RepairPipelineStage.IMPLEMENT, "implement-1", 90
        );
        assertThat(task.getStatus()).isEqualTo(RepairTaskStatus.FIXING);
        task.handoffPipelineStage(
                RepairPipelineStage.IMPLEMENT,
                RepairPipelineStage.VALIDATION,
                "implement-1",
                implementLease,
                "patch ready"
        );

        UUID validationLease = task.claimPipelineStage(
                RepairPipelineStage.VALIDATION, "validation-1", 90
        );
        task.handoffPipelineStage(
                RepairPipelineStage.VALIDATION,
                RepairPipelineStage.EXPERIENCE_MEMORY,
                "validation-1",
                validationLease,
                "validation resolved"
        );

        UUID memoryLease = task.claimPipelineStage(
                RepairPipelineStage.EXPERIENCE_MEMORY, "memory-1", 90
        );
        task.completePipeline("memory-1", memoryLease, completion(BenchmarkEvaluationStatus.RESOLVED));

        assertThat(task.getStatus()).isEqualTo(RepairTaskStatus.PATCH_READY);
        assertThat(task.getPipelineStage()).isEqualTo(RepairPipelineStage.EXPERIENCE_MEMORY);
        assertThat(task.getResultStatus()).isEqualTo(RepairResultStatus.SUCCEEDED);
        assertThat(task.getWorkerId()).isNull();
    }

    @Test
    void acceptsOnlyTheSingleFailureReviewCorrectionPath() {
        assertThat(RabbitRepairConfiguration.QUEUE)
                .isEqualTo(RepairPipelineStage.COORDINATOR.queueName());
        assertThat(RepairPipelineStage.VALIDATION.canHandoffTo(RepairPipelineStage.FAILURE_REVIEW))
                .isTrue();
        assertThat(RepairPipelineStage.FAILURE_REVIEW.canHandoffTo(RepairPipelineStage.CORRECTIVE_IMPLEMENT))
                .isTrue();
        assertThat(RepairPipelineStage.CORRECTIVE_IMPLEMENT.canHandoffTo(RepairPipelineStage.VALIDATION))
                .isTrue();
        assertThat(RepairPipelineStage.IMPLEMENT.canHandoffTo(RepairPipelineStage.CORRECTIVE_IMPLEMENT))
                .isFalse();
        assertThat(RepairPipelineStage.EXPERIENCE_MEMORY.canHandoffTo(RepairPipelineStage.IMPLEMENT))
                .isFalse();
    }

    @Test
    void refusesCompletionBeforeTheExperienceMemoryWorkerOwnsTheTask() {
        RepairTask task = queuedTask();
        UUID lease = task.claimPipelineStage(RepairPipelineStage.COORDINATOR, "coordinator-1", 90);

        assertThatThrownBy(() -> task.completePipeline(
                "coordinator-1", lease, completion(BenchmarkEvaluationStatus.RESOLVED)
        )).isInstanceOf(RepairTaskLeaseException.class);
    }

    private static RepairTask queuedTask() {
        RepairTask task = new RepairTask(null, "base-sha", "repoagent/pipeline", "pipeline-request");
        task.transitionTo(RepairTaskStatus.QUEUED);
        return task;
    }

    private static RepairTaskPipelineCompletionRequest completion(BenchmarkEvaluationStatus status) {
        return new RepairTaskPipelineCompletionRequest(
                "memory-1",
                UUID.randomUUID(),
                "Changed parser guard",
                "/runs/task/model.patch",
                "Agent completed",
                status,
                1,
                0,
                4,
                0,
                "Historical result",
                "/runs/task/evaluation.json",
                "historical-replay",
                12L,
                0L,
                0L,
                0L
        );
    }
}
