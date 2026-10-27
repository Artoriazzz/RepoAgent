package com.artoria.repoagentbackend.repair;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Instant;
import java.util.EnumSet;
import java.util.List;

@Component
public class RepairTaskRecoveryService {
    private static final Logger LOGGER = LoggerFactory.getLogger(RepairTaskRecoveryService.class);
    private static final EnumSet<RepairTaskStatus> ACTIVE_STATUSES = EnumSet.of(
            RepairTaskStatus.QUEUED,
            RepairTaskStatus.ANALYZING,
            RepairTaskStatus.FIXING,
            RepairTaskStatus.VALIDATING,
            RepairTaskStatus.PATCH_READY
    );
    private static final EnumSet<RepairTaskStatus> STRANDED_HANDOFF_STATUSES = EnumSet.of(
            RepairTaskStatus.ANALYZING,
            RepairTaskStatus.FIXING,
            RepairTaskStatus.VALIDATING
    );

    private final RepairTaskJpaRepository tasks;
    private final RepairOutboxService outbox;
    private final RepairTaskService taskService;
    private final RepairRuntimeMetrics metrics;
    private final int batchSize;
    private final int unleasedHandoffDelaySeconds;

    public RepairTaskRecoveryService(
            RepairTaskJpaRepository tasks,
            RepairOutboxService outbox,
            RepairTaskService taskService,
            RepairRuntimeMetrics metrics,
            @Value("${app.repair.recovery.batch-size:20}") int batchSize,
            @Value("${app.repair.recovery.unleased-handoff-delay-seconds:60}") int unleasedHandoffDelaySeconds
    ) {
        this.tasks = tasks;
        this.outbox = outbox;
        this.taskService = taskService;
        this.metrics = metrics;
        this.batchSize = batchSize;
        this.unleasedHandoffDelaySeconds = unleasedHandoffDelaySeconds;
    }

    @Scheduled(fixedDelayString = "${app.repair.recovery.poll-interval-millis:15000}")
    @Transactional
    public void recoverExpiredLeases() {
        Instant now = Instant.now();
        List<RepairTask> expired = tasks.findExpiredLeases(
                ACTIVE_STATUSES, now, PageRequest.of(0, batchSize)
        );
        for (RepairTask task : expired) {
            String previousWorker = task.getWorkerId();
            if (task.isCancelRequested()) {
                task.cancelAfterLeaseExpired();
                taskService.releaseTerminalCapacity(task);
                taskService.appendSystemEvent(task, RepairTaskEventType.STATUS_CHANGED, "CANCELLED",
                        "Cancelled after worker lease expired");
            } else {
                boolean redispatch = task.recoverExpiredLease();
                if (redispatch) {
                    taskService.appendSystemEvent(task, RepairTaskEventType.LEASE_EXPIRED, "QUEUED",
                            "Expired worker lease recovered from " + previousWorker);
                    outbox.enqueueStage(task, task.getPipelineStage());
                } else {
                    taskService.appendSystemEvent(task, RepairTaskEventType.LEASE_EXPIRED,
                            "EVALUATION_" + task.getEvaluationStatus().name(),
                            "Worker lease expired after patch delivery; the patch was preserved");
                }
            }
            metrics.leaseRecovered();
            LOGGER.warn("Recovered expired repair task lease taskId={} previousWorker={} status={}",
                    task.getId(), previousWorker, task.getStatus());
        }

        recoverStrandedHandoffs(now);
    }

    /**
     * 消息已被旧 Worker 确认、但下一阶段尚未领取时，不能靠租约恢复。
     * 仅迁移旧版遗留的非 QUEUED 状态，避免给正常排队的任务重复投递。
     */
    private void recoverStrandedHandoffs(Instant now) {
        Instant cutoff = now.minusSeconds(unleasedHandoffDelaySeconds);
        List<RepairTask> stranded = tasks.findStrandedUnleasedPipelineTasks(
                STRANDED_HANDOFF_STATUSES, cutoff, PageRequest.of(0, batchSize)
        );
        for (RepairTask task : stranded) {
            if (task.isCancelRequested()) {
                task.transitionTo(RepairTaskStatus.CANCELLED);
                task.recordCancellation("Task cancelled while recovering an unleased pipeline handoff");
                taskService.appendSystemEvent(task, RepairTaskEventType.STATUS_CHANGED, "CANCELLED",
                        "Cancelled while recovering an unleased pipeline handoff");
                continue;
            }
            RepairPipelineStage stage = task.getPipelineStage();
            task.prepareForReclaim();
            taskService.appendSystemEvent(task, RepairTaskEventType.PROGRESS, "QUEUED",
                    "Recovered an unleased handoff; requeued for " + stage.workerRole());
            outbox.enqueueStage(task, stage);
            LOGGER.warn("Recovered stranded repair pipeline handoff taskId={} stage={}", task.getId(), stage);
        }
    }
}
