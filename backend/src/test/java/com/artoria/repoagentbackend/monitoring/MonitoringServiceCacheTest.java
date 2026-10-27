package com.artoria.repoagentbackend.monitoring;

import com.artoria.repoagentbackend.common.RedisJsonCache;
import com.artoria.repoagentbackend.repair.RepairResultStatus;
import com.artoria.repoagentbackend.repair.RepairStageStatus;
import com.artoria.repoagentbackend.repair.RepairTaskEventJpaRepository;
import com.artoria.repoagentbackend.repair.RepairTaskJpaRepository;
import com.artoria.repoagentbackend.repair.RepairTaskStatus;
import com.artoria.repoagentbackend.repair.RepairWorkflowStatus;
import com.artoria.repoagentbackend.repair.WorkerNodeJpaRepository;
import com.artoria.repoagentbackend.repair.WorkerPresenceStore;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.Instant;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class MonitoringServiceCacheTest {
    @Mock RepairTaskJpaRepository tasks;
    @Mock WorkerNodeJpaRepository workers;
    @Mock RepairTaskEventJpaRepository events;
    @Mock WorkerPresenceStore workerPresence;
    @Mock RedisJsonCache cache;

    @Test
    void overviewCacheHitSkipsDatabaseAggregation() {
        UUID actorId = UUID.randomUUID();
        MonitoringOverviewResponse expected = new MonitoringOverviewResponse(
                Instant.parse("2026-09-22T00:00:00Z"),
                2, 1, 4, 1, 3, 0, true, 1200, 8000,
                Map.of(RepairTaskStatus.QUEUED, 2L),
                Map.of(RepairTaskStatus.QUEUED, 2L),
                Map.of("IMPLEMENT", 1L),
                Map.of("COORDINATOR", 2L),
                Map.of(RepairResultStatus.SUCCEEDED, 4L),
                Map.of(RepairWorkflowStatus.RUNNING, 3L),
                Map.of(RepairStageStatus.WAITING, 2L)
        );
        when(cache.get("monitoring:overview:v2:" + actorId, MonitoringOverviewResponse.class))
                .thenReturn(Optional.of(expected));
        MonitoringService service = new MonitoringService(
                tasks, workers, events, workerPresence, cache,
                45, 2000, 30000, 5000, 0, 20, 4
        );

        assertThat(service.overview(actorId)).isSameAs(expected);
        verifyNoInteractions(tasks, workers, events, workerPresence);
    }

    @Test
    void overviewUsesStaleValueWhenAnotherRequestIsRebuildingTheCache() {
        UUID actorId = UUID.randomUUID();
        MonitoringOverviewResponse stale = new MonitoringOverviewResponse(
                Instant.parse("2026-09-22T00:00:00Z"),
                2, 1, 4, 1, 3, 0, true, 1200, 8000,
                Map.of(), Map.of(), Map.of(), Map.of(), Map.of(), Map.of(), Map.of()
        );
        String cacheKey = "monitoring:overview:v2:" + actorId;
        when(cache.get(cacheKey, MonitoringOverviewResponse.class)).thenReturn(Optional.empty());
        when(cache.tryAcquireLock(anyString(), anyString(), org.mockito.ArgumentMatchers.any()))
                .thenReturn(false);
        when(cache.get(cacheKey + ":stale", MonitoringOverviewResponse.class))
                .thenReturn(Optional.of(stale));
        MonitoringService service = new MonitoringService(
                tasks, workers, events, workerPresence, cache,
                45, 2000, 30000, 5000, 0, 20, 4
        );

        assertThat(service.overview(actorId)).isSameAs(stale);
        verifyNoInteractions(tasks, workers, events, workerPresence);
    }
}
