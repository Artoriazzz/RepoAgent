package com.artoria.repoagentbackend.repair;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.springframework.stereotype.Component;

import java.util.EnumSet;

@Component
public class RepairRuntimeMetrics {
    private final Counter outboxPublished;
    private final Counter outboxFailed;
    private final Counter leasesRecovered;

    public RepairRuntimeMetrics(MeterRegistry registry, RepairOutboxJpaRepository outbox) {
        outboxPublished = registry.counter("repoagent.outbox.published");
        outboxFailed = registry.counter("repoagent.outbox.failed");
        leasesRecovered = registry.counter("repoagent.task.leases.recovered");
        registry.gauge("repoagent.outbox.backlog", outbox,
                repository -> repository.countByStatusIn(
                        EnumSet.of(RepairOutboxStatus.PENDING, RepairOutboxStatus.FAILED)
                ));
        registry.gauge("repoagent.outbox.dead", outbox,
                repository -> repository.countByStatus(RepairOutboxStatus.DEAD));
    }

    public void outboxPublished() { outboxPublished.increment(); }
    public void outboxFailed() { outboxFailed.increment(); }
    public void leaseRecovered() { leasesRecovered.increment(); }
}
