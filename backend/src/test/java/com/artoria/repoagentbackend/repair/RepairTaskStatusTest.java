package com.artoria.repoagentbackend.repair;

import org.junit.jupiter.api.Test;

import java.util.UUID;
import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;

class RepairTaskStatusTest {

    @Test
    void followsTheExpectedRepairPath() {
        assertThat(RepairTaskStatus.CREATED.canTransitionTo(RepairTaskStatus.QUEUED)).isTrue();
        assertThat(RepairTaskStatus.QUEUED.canTransitionTo(RepairTaskStatus.ANALYZING)).isTrue();
        assertThat(RepairTaskStatus.QUEUED.canTransitionTo(RepairTaskStatus.FIXING)).isTrue();
        assertThat(RepairTaskStatus.QUEUED.canTransitionTo(RepairTaskStatus.VALIDATING)).isTrue();
        assertThat(RepairTaskStatus.ANALYZING.canTransitionTo(RepairTaskStatus.FIXING)).isTrue();
        assertThat(RepairTaskStatus.FIXING.canTransitionTo(RepairTaskStatus.VALIDATING)).isTrue();
        assertThat(RepairTaskStatus.VALIDATING.canTransitionTo(RepairTaskStatus.PATCH_READY)).isTrue();
        assertThat(RepairTaskStatus.PATCH_READY.canTransitionTo(RepairTaskStatus.PR_READY)).isTrue();
    }

    @Test
    void rejectsTerminalAndSkippedTransitions() {
        assertThat(RepairTaskStatus.CREATED.canTransitionTo(RepairTaskStatus.PR_READY)).isFalse();
        assertThat(RepairTaskStatus.PR_READY.canTransitionTo(RepairTaskStatus.FIXING)).isFalse();
    }

    @Test
    void retainsRepairArtifactsWhenTheTaskCompletes() {
        RepairTask task = new RepairTask(null, "base-sha", "repoagent/issue-1/run-1", "request-1");
        task.recordResult(
                "Changed parser validation",
                "/runs/task/model.patch",
                "mvn test passed",
                "https://github.com/octo/demo/pull/1"
        );

        assertThat(task.getPatchSummary()).isEqualTo("Changed parser validation");
        assertThat(task.getPatchArtifactPath()).isEqualTo("/runs/task/model.patch");
        assertThat(task.getValidationSummary()).isEqualTo("mvn test passed");
        assertThat(task.getPrUrl()).isEqualTo("https://github.com/octo/demo/pull/1");
        assertThat(task.getCompletedAt()).isNotNull();
    }

    @Test
    void keepsTheIdUsedToBuildTheBranchName() {
        UUID taskId = UUID.randomUUID();
        RepairTask task = new RepairTask(
                taskId, null, "base-sha", "repoagent/run-" + taskId, "request-1"
        );

        assertThat(task.getId()).isEqualTo(taskId);
    }

    @Test
    void fencesUpdatesWithAWorkerLease() {
        RepairTask task = new RepairTask(null, "base-sha", "repoagent/run-1", "request-1");
        UUID leaseToken = task.claim("worker-a", 90);

        task.requireLease("worker-a", leaseToken);

        org.assertj.core.api.Assertions.assertThatThrownBy(
                () -> task.requireLease("worker-b", leaseToken)
        ).isInstanceOf(RepairTaskLeaseException.class);
    }

    @Test
    void recordsCancellationAndRetryLineage() {
        RepairTask original = new RepairTask(
                UUID.randomUUID(), null, "base-sha", "repoagent/run-1", "request-1"
        );
        original.requestCancellation();
        original.recordCancellation("Stopped by owner");
        RepairTask retry = RepairTask.retryOf(
                UUID.randomUUID(), original, "repoagent/run-2", "request-2"
        );

        assertThat(original.isCancelRequested()).isTrue();
        assertThat(original.getCompletedAt()).isNotNull();
        assertThat(retry.getParentTaskId()).isEqualTo(original.getId());
        assertThat(retry.getAttemptNumber()).isEqualTo(2);
    }

    @Test
    void recoversAnExpiredActiveLeaseForRedispatch() {
        RepairTask task = new RepairTask(null, "base-sha", "repoagent/run-3", "request-3");
        task.transitionTo(RepairTaskStatus.QUEUED);
        task.claim("worker-a", 90);
        task.transitionTo(RepairTaskStatus.ANALYZING);

        boolean redispatch = task.recoverExpiredLease(Instant.now().plusSeconds(91));

        assertThat(redispatch).isTrue();
        assertThat(task.getStatus()).isEqualTo(RepairTaskStatus.QUEUED);
        assertThat(task.getWorkerId()).isNull();
        assertThat(task.getLeaseToken()).isNull();
        assertThat(task.getLeaseExpiresAt()).isNull();
    }

    @Test
    void preservesThePatchWhenAHarnessLeaseExpires() {
        RepairTask task = new RepairTask(null, "base-sha", "repoagent/run-4", "request-4");
        task.transitionTo(RepairTaskStatus.QUEUED);
        task.claim("worker-a", 90);
        task.transitionTo(RepairTaskStatus.ANALYZING);
        task.transitionTo(RepairTaskStatus.FIXING);
        task.transitionTo(RepairTaskStatus.VALIDATING);
        task.transitionTo(RepairTaskStatus.PATCH_READY);
        task.recordResult("patch", "/runs/task/model.patch", "generated", null);
        task.recordEvaluation(new BenchmarkEvaluationUpdateRequest(
                BenchmarkEvaluationStatus.RUNNING, null, null, null, null,
                null, null, "worker-a", task.getLeaseToken()
        ));

        boolean redispatch = task.recoverExpiredLease(Instant.now().plusSeconds(91));

        assertThat(redispatch).isFalse();
        assertThat(task.getStatus()).isEqualTo(RepairTaskStatus.PATCH_READY);
        assertThat(task.getPatchArtifactPath()).isEqualTo("/runs/task/model.patch");
        assertThat(task.getEvaluationStatus()).isEqualTo(BenchmarkEvaluationStatus.HARNESS_ERROR);
        assertThat(task.getWorkerId()).isNull();
    }

    @Test
    void classifiesAnUnclaimedHistoricalPatchWithoutEvaluationAsEnvironmentError() {
        RepairTask task = new RepairTask(null, "base-sha", "repoagent/legacy", "request-legacy");
        task.transitionTo(RepairTaskStatus.QUEUED);
        task.transitionTo(RepairTaskStatus.ANALYZING);
        task.transitionTo(RepairTaskStatus.FIXING);
        task.transitionTo(RepairTaskStatus.VALIDATING);
        task.transitionTo(RepairTaskStatus.PATCH_READY);
        task.recordResult("patch", "/legacy/model.patch", "static checks passed", null);

        assertThat(task.getEvaluationStatus()).isEqualTo(BenchmarkEvaluationStatus.NOT_RUN);
        assertThat(task.getResultStatus()).isEqualTo(RepairResultStatus.ENVIRONMENT_ERROR);
    }
}
