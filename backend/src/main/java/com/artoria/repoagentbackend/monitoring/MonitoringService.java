package com.artoria.repoagentbackend.monitoring;

import com.artoria.repoagentbackend.common.RedisJsonCache;
import com.artoria.repoagentbackend.common.ServiceUnavailableException;
import com.artoria.repoagentbackend.repair.RepairTask;
import com.artoria.repoagentbackend.repair.RepairTaskEventJpaRepository;
import com.artoria.repoagentbackend.repair.RepairTaskEventResponse;
import com.artoria.repoagentbackend.repair.RepairTaskEventType;
import com.artoria.repoagentbackend.repair.RepairTaskJpaRepository;
import com.artoria.repoagentbackend.repair.RepairResultStatus;
import com.artoria.repoagentbackend.repair.RepairTaskStatus;
import com.artoria.repoagentbackend.repair.RepairStageStatus;
import com.artoria.repoagentbackend.repair.RepairWorkflowStatus;
import com.artoria.repoagentbackend.repair.WorkerNode;
import com.artoria.repoagentbackend.repair.WorkerNodeJpaRepository;
import com.artoria.repoagentbackend.repair.WorkerPresence;
import com.artoria.repoagentbackend.repair.WorkerPresenceSnapshot;
import com.artoria.repoagentbackend.repair.WorkerPresenceStore;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.Comparator;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Function;
import java.util.stream.Collectors;

@Service
public class MonitoringService {
    private final RepairTaskJpaRepository tasks;
    private final WorkerNodeJpaRepository workers;
    private final RepairTaskEventJpaRepository events;
    private final Duration workerOfflineAfter;
    private final WorkerPresenceStore workerPresence;
    private final RedisJsonCache cache;
    private final Duration overviewCacheTtl;
    private final Duration overviewStaleTtl;
    private final Duration overviewRebuildLockTtl;
    private final Duration overviewRebuildWait;
    private final int overviewTtlJitterPercent;
    private final Semaphore fallbackOverviewQueries;

    public MonitoringService(
            RepairTaskJpaRepository tasks,
            WorkerNodeJpaRepository workers,
            RepairTaskEventJpaRepository events,
            WorkerPresenceStore workerPresence,
            RedisJsonCache cache,
            @Value("${app.monitoring.worker-offline-after-seconds:45}") long workerOfflineAfterSeconds,
            @Value("${app.cache.monitoring-overview-ttl-millis:3000}") long overviewCacheTtlMillis,
            @Value("${app.cache.monitoring-overview-stale-ttl-millis:30000}") long overviewStaleTtlMillis,
            @Value("${app.cache.monitoring-overview-rebuild-lock-ttl-millis:5000}") long overviewRebuildLockTtlMillis,
            @Value("${app.cache.monitoring-overview-rebuild-wait-millis:120}") long overviewRebuildWaitMillis,
            @Value("${app.cache.monitoring-overview-ttl-jitter-percent:20}") int overviewTtlJitterPercent,
            @Value("${app.cache.monitoring-overview-fallback-max-concurrency:4}") int fallbackOverviewQueries
    ) {
        this.tasks = tasks;
        this.workers = workers;
        this.events = events;
        this.workerPresence = workerPresence;
        this.cache = cache;
        this.workerOfflineAfter = Duration.ofSeconds(workerOfflineAfterSeconds);
        this.overviewCacheTtl = Duration.ofMillis(Math.max(100, overviewCacheTtlMillis));
        this.overviewStaleTtl = Duration.ofMillis(Math.max(overviewCacheTtlMillis, overviewStaleTtlMillis));
        this.overviewRebuildLockTtl = Duration.ofMillis(Math.max(100, overviewRebuildLockTtlMillis));
        this.overviewRebuildWait = Duration.ofMillis(Math.max(0, overviewRebuildWaitMillis));
        this.overviewTtlJitterPercent = Math.max(0, Math.min(50, overviewTtlJitterPercent));
        this.fallbackOverviewQueries = new Semaphore(Math.max(1, fallbackOverviewQueries));
    }

    @Transactional(readOnly = true)
    public MonitoringOverviewResponse overview(UUID actorId) {
        String cacheKey = "monitoring:overview:v2:" + actorId;
        MonitoringOverviewResponse cached = readOverview(cacheKey);
        if (cached != null) return cached;

        String lockKey = cacheKey + ":rebuild";
        String lockToken = UUID.randomUUID().toString();
        if (cache.tryAcquireLock(lockKey, lockToken, overviewRebuildLockTtl)) {
            try {
                cached = readOverview(cacheKey);
                if (cached != null) return cached;
                return rebuildOverview(actorId, cacheKey);
            } finally {
                cache.releaseLock(lockKey, lockToken);
            }
        }

        cached = waitForOverview(cacheKey);
        if (cached != null) return cached;
        cached = cache.get(staleOverviewKey(cacheKey), MonitoringOverviewResponse.class).orElse(null);
        if (cached != null) return cached;

        if (!fallbackOverviewQueries.tryAcquire()) {
            throw new ServiceUnavailableException("Monitoring summary is being refreshed; retry shortly");
        }
        try {
            return rebuildOverview(actorId, cacheKey);
        } finally {
            fallbackOverviewQueries.release();
        }
    }

    private MonitoringOverviewResponse rebuildOverview(UUID actorId, String cacheKey) {
        MonitoringOverviewResponse response = buildOverview(actorId);
        cache.put(cacheKey, response, jitteredOverviewTtl());
        cache.put(staleOverviewKey(cacheKey), response, overviewStaleTtl);
        return response;
    }

    private MonitoringOverviewResponse readOverview(String cacheKey) {
        return cache.get(cacheKey, MonitoringOverviewResponse.class).orElse(null);
    }

    private MonitoringOverviewResponse waitForOverview(String cacheKey) {
        long deadline = System.nanoTime() + overviewRebuildWait.toNanos();
        while (System.nanoTime() < deadline) {
            MonitoringOverviewResponse response = readOverview(cacheKey);
            if (response != null) return response;
            try {
                Thread.sleep(Math.min(20, Math.max(1, overviewRebuildWait.toMillis())));
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                return null;
            }
        }
        return readOverview(cacheKey);
    }

    private Duration jitteredOverviewTtl() {
        if (overviewTtlJitterPercent == 0) return overviewCacheTtl;
        long millis = overviewCacheTtl.toMillis();
        long spread = Math.max(1, millis * overviewTtlJitterPercent / 100);
        return Duration.ofMillis(Math.max(1, millis + ThreadLocalRandom.current().nextLong(-spread, spread + 1)));
    }

    private static String staleOverviewKey(String cacheKey) {
        return cacheKey + ":stale";
    }

    private MonitoringOverviewResponse buildOverview(UUID actorId) {

        Instant now = Instant.now();
        Instant startOfToday = LocalDate.now(ZoneId.systemDefault())
                .atStartOfDay(ZoneId.systemDefault()).toInstant();
        List<RepairTask> accessible = tasks.findAccessibleForStatistics(actorId, null, null);
        Map<UUID, Instant> firstClaimedAt = accessible.isEmpty()
                ? Map.of()
                : events.findFirstEventTimes(
                        accessible.stream().map(RepairTask::getId).toList(),
                        RepairTaskEventType.LEASE_CLAIMED
                ).stream().collect(Collectors.toMap(
                        row -> (UUID) row[0],
                        row -> (Instant) row[1]
                ));
        Map<RepairTaskStatus, Long> byStatus = new EnumMap<>(RepairTaskStatus.class);
        Map<RepairTaskStatus, Long> byActiveStatus = new EnumMap<>(RepairTaskStatus.class);
        Map<String, Long> byActiveStage = new java.util.LinkedHashMap<>();
        Map<String, Long> byQueuedPipelineStage = new java.util.LinkedHashMap<>();
        Map<RepairResultStatus, Long> byResultStatus = new EnumMap<>(RepairResultStatus.class);
        Map<RepairWorkflowStatus, Long> byWorkflowStatus = new EnumMap<>(RepairWorkflowStatus.class);
        Map<RepairStageStatus, Long> byStageStatus = new EnumMap<>(RepairStageStatus.class);
        long queued = 0;
        long active = 0;
        long completedToday = 0;
        long failedToday = 0;
        long queueWaitTotal = 0;
        long queueWaitSamples = 0;
        long executionTotal = 0;
        long executionSamples = 0;
        for (RepairTask task : accessible) {
            byStatus.merge(task.getStatus(), 1L, Long::sum);
            byWorkflowStatus.merge(task.getWorkflowStatus(), 1L, Long::sum);
            byStageStatus.merge(task.getStageStatus(), 1L, Long::sum);
            RepairResultStatus resultStatus = task.getResultStatus();
            if (task.getWorkflowStatus() == RepairWorkflowStatus.RUNNING) {
                if (task.getStageStatus() == RepairStageStatus.RUNNING && hasActiveLease(task, now)) {
                    RepairTaskStatus activeStatus = task.getStatus() == RepairTaskStatus.CREATED ? RepairTaskStatus.QUEUED : task.getStatus();
                    byActiveStatus.merge(activeStatus, 1L, Long::sum);
                    byActiveStage.merge(task.getCurrentPipelineStage().name(), 1L, Long::sum);
                    active++;
                } else {
                    byActiveStatus.merge(RepairTaskStatus.QUEUED, 1L, Long::sum);
                    byQueuedPipelineStage.merge(task.getCurrentPipelineStage().name(), 1L, Long::sum);
                    queued++;
                }
            } else {
                byResultStatus.merge(resultStatus, 1L, Long::sum);
            }
            if (resultStatus != RepairResultStatus.PENDING
                    && task.getCompletedAt() != null
                    && !task.getCompletedAt().isBefore(startOfToday)) {
                completedToday++;
                if (resultStatus == RepairResultStatus.FAILED
                        || resultStatus == RepairResultStatus.ENVIRONMENT_ERROR) {
                    failedToday++;
                }
            }
            Instant claimedAt = firstClaimedAt.get(task.getId());
            if (task.getCreatedAt() != null && claimedAt != null) {
                queueWaitTotal += Math.max(0, Duration.between(
                        task.getCreatedAt(), claimedAt
                ).toMillis());
                queueWaitSamples++;
            }
            if (task.getDurationMs() != null) {
                executionTotal += task.getDurationMs();
                executionSamples++;
            }
        }
        WorkerPresenceSnapshot presence = workerPresence.current();
        WorkerCounts workerCounts = workerCounts(workers.findAll(), presence, now);
        MonitoringOverviewResponse response = new MonitoringOverviewResponse(
                now, queued, active, completedToday, failedToday,
                workerCounts.online(), workerCounts.offline(), workerCounts.available(),
                queueWaitSamples == 0 ? 0 : queueWaitTotal / queueWaitSamples,
                executionSamples == 0 ? 0 : executionTotal / executionSamples,
                byStatus, byActiveStatus, byActiveStage, byQueuedPipelineStage, byResultStatus,
                byWorkflowStatus, byStageStatus
        );
        return response;
    }

    private boolean hasActiveLease(RepairTask task, Instant now) {
        return task.getWorkerId() != null
                && task.getLeaseExpiresAt() != null
                && task.getLeaseExpiresAt().isAfter(now);
    }

    private String queuedPipelineStage(RepairTask task) {
        return task.getPipelineStage() == null ? "QUEUED" : task.getPipelineStage().name();
    }

    private String activeStage(RepairTask task, RepairTaskStatus status) {
        if (status == RepairTaskStatus.QUEUED) return "QUEUED";
        String stage = task.getCurrentStage();
        if (stage == null || stage.isBlank()) return status.name();
        return switch (stage) {
            case "FAILURE_REVIEW", "CORRECTIVE_IMPLEMENT", "RETRYING" -> "RETRYING";
            case "EXPERIENCE_MEMORY", "MEMORY_OBSERVED", "MEMORY_CURATED" -> "EXPERIENCE_MEMORY";
            case "ANALYZING", "COORDINATOR", "MEMORY_RECALLED", "MEMORY_RECALL_SKIPPED" -> "ANALYZING";
            case "IMPLEMENT", "RESEARCH" -> "FIXING";
            case "PUBLIC_VALIDATION", "PATCH_REVIEW", "VERIFY", "TEST_EXPANDER",
                    "VALIDATING", "FINAL_EVALUATION" -> "VALIDATING";
            default -> stage;
        };
    }

    @Transactional(readOnly = true)
    public List<MonitoringWorkerResponse> workers(UUID actorId) {
        Instant now = Instant.now();
        Map<UUID, RepairTask> accessibleTasks = tasks.findAccessibleForStatistics(actorId, null, null)
                .stream().collect(Collectors.toMap(RepairTask::getId, Function.identity()));
        WorkerPresenceSnapshot presence = workerPresence.current();
        Map<String, WorkerNode> registered = workers.findAll().stream()
                .collect(Collectors.toMap(WorkerNode::getWorkerId, Function.identity(), (left, right) -> left));
        Set<String> workerIds = new TreeSet<>(registered.keySet());
        workerIds.addAll(presence.workers().keySet());
        return workerIds.stream()
                .map(workerId -> workerResponse(
                        registered.get(workerId), presence.workers().get(workerId),
                        accessibleTasks, presence.available(), now
                ))
                .sorted(Comparator
                        .comparing((MonitoringWorkerResponse worker) -> !"ONLINE".equals(worker.availability()))
                        .thenComparing(MonitoringWorkerResponse::workerId))
                .toList();
    }

    private MonitoringWorkerResponse workerResponse(
            WorkerNode worker,
            WorkerPresence presence,
            Map<UUID, RepairTask> accessibleTasks,
            boolean presenceAvailable,
            Instant now
    ) {
        boolean legacyOnline = worker != null && worker.getInstanceId() == null && isOnline(worker, now);
        String availability = !presenceAvailable
                ? "UNAVAILABLE"
                : presence != null || legacyOnline ? "ONLINE" : "OFFLINE";
        UUID activeTaskId = presence != null
                ? presence.activeTaskId()
                : worker == null ? null : worker.getActiveTaskId();
        RepairTask task = activeTaskId == null ? null : accessibleTasks.get(activeTaskId);
        RepairTaskEventResponse latest = task == null ? null : events.findFirstByTaskIdOrderByCreatedAtDesc(task.getId())
                .map(RepairTaskEventResponse::from)
                .orElse(null);
        return new MonitoringWorkerResponse(
                presence != null ? presence.workerId() : worker.getWorkerId(),
                presence != null ? presence.role() : worker == null ? null : worker.getWorkerRole(),
                availability,
                presence != null ? presence.status() : worker == null ? "UNAVAILABLE" : worker.getStatus(),
                presence != null ? presence.lastHeartbeatAt() : worker == null ? null : worker.getLastHeartbeatAt(),
                task == null ? null : activeTaskId,
                task == null ? null : task.getStatus(),
                latest == null ? null : latest.stage(),
                latest == null ? null : latest.message(),
                task == null ? null : task.getIssue().getRepository().getOwnerLogin()
                        + "/" + task.getIssue().getRepository().getName(),
                task == null ? null : task.getIssue().getTitle(),
                task == null ? null : task.getCreatedAt()
        );
    }

    private boolean isOnline(WorkerNode worker, Instant now) {
        return worker.getLastHeartbeatAt() != null
                && worker.getLastHeartbeatAt().isAfter(now.minus(workerOfflineAfter));
    }

    private WorkerCounts workerCounts(List<WorkerNode> registered, WorkerPresenceSnapshot presence, Instant now) {
        if (!presence.available()) return new WorkerCounts(0, 0, false);
        Set<String> known = new java.util.HashSet<>(presence.workers().keySet());
        long online = presence.workers().size();
        for (WorkerNode worker : registered) {
            known.add(worker.getWorkerId());
            if (!presence.workers().containsKey(worker.getWorkerId())
                    && worker.getInstanceId() == null
                    && isOnline(worker, now)) {
                online++;
            }
        }
        return new WorkerCounts(online, Math.max(0, known.size() - online), true);
    }

    private record WorkerCounts(long online, long offline, boolean available) {
    }
}
