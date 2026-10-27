package com.artoria.repoagentbackend.repair;

import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class RepairTaskWorkflowStateTest {

    @Test
    void handoffAdvancesSequenceAndFencesTheOldWorker() {
        RepairTask task = new RepairTask(UUID.randomUUID(), null, "base", "repoagent/fencing", "request");
        task.admit(UUID.randomUUID());
        task.transitionTo(RepairTaskStatus.QUEUED);

        UUID coordinatorLease = task.claimPipelineStage(
                RepairPipelineStage.COORDINATOR, 0, "coordinator-1", 90
        );
        long coordinatorEpoch = task.getLeaseEpoch();
        task.handoffPipelineStage(
                RepairPipelineStage.COORDINATOR, RepairPipelineStage.IMPLEMENT,
                "coordinator-1", coordinatorLease, 0, coordinatorEpoch, "brief complete"
        );

        assertThat(task.getWorkflowStatus()).isEqualTo(RepairWorkflowStatus.RUNNING);
        assertThat(task.getStageStatus()).isEqualTo(RepairStageStatus.WAITING);
        assertThat(task.getStageSequence()).isEqualTo(1);
        assertThat(task.getCurrentPipelineStage()).isEqualTo(RepairPipelineStage.IMPLEMENT);
        assertThatThrownBy(() -> task.requireLease("coordinator-1", coordinatorLease, 0, coordinatorEpoch))
                .isInstanceOf(RepairTaskLeaseException.class);
    }

    @Test
    void terminalCapacityMarkerIsIdempotent() {
        RepairTask task = new RepairTask(UUID.randomUUID(), null, "base", "repoagent/cancel", "request");
        task.admit(UUID.randomUUID());
        task.transitionTo(RepairTaskStatus.QUEUED);
        task.transitionTo(RepairTaskStatus.CANCELLED);

        assertThat(task.getWorkflowStatus()).isEqualTo(RepairWorkflowStatus.CANCELLED);
        assertThat(task.markCapacityReleasedIfTerminal()).isTrue();
        assertThat(task.markCapacityReleasedIfTerminal()).isFalse();
    }
}
