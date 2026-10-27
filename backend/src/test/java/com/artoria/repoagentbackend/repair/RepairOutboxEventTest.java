package com.artoria.repoagentbackend.repair;

import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

class RepairOutboxEventTest {
    @Test
    void retriesWithBackoffBeforeBecomingDead() {
        RepairOutboxEvent event = new RepairOutboxEvent(
                UUID.randomUUID(), RepairOutboxEvent.TASK_QUEUED, "{}"
        );

        event.markFailed("rabbit unavailable", 2);
        assertThat(event.getStatus()).isEqualTo(RepairOutboxStatus.FAILED);
        assertThat(event.getAttempts()).isEqualTo(1);
        assertThat(event.getNextAttemptAt()).isNotNull();

        event.markFailed("rabbit still unavailable", 2);
        assertThat(event.getStatus()).isEqualTo(RepairOutboxStatus.DEAD);
        assertThat(event.getAttempts()).isEqualTo(2);
    }

    @Test
    void reschedulesAPublishedEventForLeaseRecovery() {
        RepairOutboxEvent event = new RepairOutboxEvent(
                UUID.randomUUID(), RepairOutboxEvent.TASK_QUEUED, "{\"attempt\":1}"
        );
        event.markPublished();

        event.reschedule("{\"attempt\":2}");

        assertThat(event.getStatus()).isEqualTo(RepairOutboxStatus.PENDING);
        assertThat(event.getAttempts()).isZero();
        assertThat(event.getPublishedAt()).isNull();
        assertThat(event.getPayloadJson()).isEqualTo("{\"attempt\":2}");
    }
}
