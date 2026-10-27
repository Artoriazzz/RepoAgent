package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.RedisJsonCache;
import com.artoria.repoagentbackend.common.ConflictException;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.EnumSet;
import java.util.UUID;

/**
 * 新工作流唯一的准入点。全局容量行先加悲观锁，令用户运行计数和当天额度的
 * 首次创建也不会发生竞争；这比依赖前端计数可靠。
 */
@Service
public class RepairAdmissionService {
    private final RepairUserQuotaJpaRepository quotas;
    private final RepairUserRuntimeJpaRepository runtimes;
    private final RepairDailyUsageJpaRepository dailyUsage;
    private final RepairSystemCapacityJpaRepository capacities;
    private final RepairTaskJpaRepository tasks;
    private final RepairOutboxJpaRepository outbox;
    private final RepairStageExecutionJpaRepository executions;
    private final RepairAdmissionProperties properties;
    private final ObjectProvider<StringRedisTemplate> redis;
    private final RedisJsonCache cache;
    private final Duration statusCacheTtl;

    public RepairAdmissionService(
            RepairUserQuotaJpaRepository quotas,
            RepairUserRuntimeJpaRepository runtimes,
            RepairDailyUsageJpaRepository dailyUsage,
            RepairSystemCapacityJpaRepository capacities,
            RepairTaskJpaRepository tasks,
            RepairOutboxJpaRepository outbox,
            RepairStageExecutionJpaRepository executions,
            RepairAdmissionProperties properties,
            ObjectProvider<StringRedisTemplate> redis,
            RedisJsonCache cache,
            @org.springframework.beans.factory.annotation.Value("${app.cache.admission-status-ttl-millis:2000}")
            long statusCacheTtlMillis
    ) {
        this.quotas = quotas; this.runtimes = runtimes; this.dailyUsage = dailyUsage;
        this.capacities = capacities; this.tasks = tasks; this.outbox = outbox;
        this.executions = executions; this.properties = properties; this.redis = redis;
        this.cache = cache; this.statusCacheTtl = Duration.ofMillis(statusCacheTtlMillis);
    }

    /** 限制的是 HTTP 突发，不影响数据库事实来源；Redis 异常时交由数据库背压兜底。 */
    public void checkRequestRate(UUID userId) {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return;
        try {
            long minute = Instant.now().getEpochSecond() / 60;
            String key = "repair:admission:rate:" + userId + ":" + minute;
            Long current = template.opsForValue().increment(key);
            if (current != null && current == 1) template.expire(key, Duration.ofMinutes(2));
            if (current != null && current > properties.getRequestsPerMinute()) {
                throw new ConflictException("Submission rate limit reached; retry in the next minute");
            }
        } catch (ConflictException error) {
            throw error;
        } catch (RuntimeException ignored) {
            // Redis is an acceleration layer. Do not make a healthy transactional admission unavailable.
        }
    }

    @Transactional
    public void reserve(UUID actorId, RepairTask task) {
        RepairSystemCapacity global = capacities.findByCapacityKeyForUpdate(RepairSystemCapacity.GLOBAL)
                .orElseGet(() -> capacities.saveAndFlush(new RepairSystemCapacity(RepairSystemCapacity.GLOBAL)));
        if (global.getActiveWorkflows() >= properties.getGlobalMaxInflight()) {
            throw new ConflictException("Global repair capacity is full; please retry later");
        }
        long waiting = tasks.countByWorkflowStatusAndStageStatus(
                RepairWorkflowStatus.RUNNING, RepairStageStatus.WAITING
        );
        if (waiting >= properties.getGlobalMaxWaitingStages()) {
            throw new ConflictException("Repair stage queue is at capacity; please retry later");
        }
        long backlog = outbox.countByStatusIn(EnumSet.of(RepairOutboxStatus.PENDING, RepairOutboxStatus.FAILED));
        if (backlog >= properties.getOutboxBacklogLimit()) {
            throw new ConflictException("Repair dispatch backlog is full; please retry later");
        }

        RepairUserQuota quota = quotas.findById(actorId).orElse(null);
        int maxActive = quota == null ? properties.getDefaultMaxActiveWorkflows() : quota.getMaxActiveWorkflows();
        int dailyLimit = quota == null ? properties.getDefaultDailyTaskLimit() : quota.getDailyTaskLimit();
        if (quota != null && !quota.isEnabled()) throw new ConflictException("Repair submissions are disabled for this user");

        RepairUserRuntime runtime = runtimes.findByUserIdForUpdate(actorId)
                .orElseGet(() -> runtimes.saveAndFlush(new RepairUserRuntime(actorId)));
        if (runtime.getActiveWorkflows() >= maxActive) {
            throw new ConflictException("User active workflow limit reached (max " + maxActive + ")");
        }
        LocalDate today = LocalDate.now(ZoneOffset.UTC);
        RepairDailyUsage usage = dailyUsage.findForUpdate(actorId, today)
                .orElseGet(() -> dailyUsage.saveAndFlush(new RepairDailyUsage(actorId, today)));
        if (usage.getAcceptedCount() >= dailyLimit) {
            throw new ConflictException("Daily repair quota reached; it resets at 00:00 UTC");
        }
        runtime.reserve();
        usage.accept();
        global.reserve();
        task.admit(actorId);
        cache.evict(statusCacheKey(actorId));
    }

    @Transactional
    public void recordInitialStage(RepairTask task) {
        executions.save(new RepairStageExecution(task.getId(), task.getStageSequence(), task.getPipelineStage()));
    }

    @Transactional
    public void recordClaim(RepairTask task) {
        RepairStageExecution execution = executions.findForUpdate(task.getId(), task.getStageSequence())
                .orElseGet(() -> executions.saveAndFlush(new RepairStageExecution(
                        task.getId(), task.getStageSequence(), task.getPipelineStage())));
        execution.start(task.getLeaseEpoch(), task.getWorkerId());
    }

    @Transactional
    public void recordHandoff(RepairTask task, long completedSequence, RepairPipelineStage completedStage) {
        executions.findForUpdate(task.getId(), completedSequence).ifPresent(RepairStageExecution::succeed);
        executions.save(new RepairStageExecution(task.getId(), task.getStageSequence(), task.getPipelineStage()));
    }

    @Transactional
    public void recordStageFailure(RepairTask task, String errorCode, String errorSummary) {
        executions.findForUpdate(task.getId(), task.getStageSequence())
                .ifPresent(execution -> execution.fail(errorCode, errorSummary));
    }

    @Transactional
    public void recordStageSucceeded(RepairTask task) {
        executions.findForUpdate(task.getId(), task.getStageSequence())
                .ifPresent(RepairStageExecution::succeed);
    }

    /** 终态释放在同一事务写 capacity_released_at，重复调用不会重复扣减。 */
    @Transactional
    public void releaseTerminalCapacity(RepairTask task) {
        if (!task.getWorkflowStatus().isTerminal() || task.getCapacityReleasedAt() != null) return;
        RepairSystemCapacity global = capacities.findByCapacityKeyForUpdate(RepairSystemCapacity.GLOBAL)
                .orElseThrow(() -> new IllegalStateException("Global repair capacity row is missing"));
        UUID userId = task.getSubmittedByUserId();
        if (userId == null || !task.markCapacityReleasedIfTerminal()) return;
        runtimes.findByUserIdForUpdate(userId).ifPresent(RepairUserRuntime::release);
        global.release();
        cache.evict(statusCacheKey(userId));
    }

    @Transactional(readOnly = true)
    public RepairAdmissionStatusResponse status(UUID actorId) {
        String cacheKey = statusCacheKey(actorId);
        RepairAdmissionStatusResponse cached = cache.get(
                cacheKey, RepairAdmissionStatusResponse.class
        ).orElse(null);
        if (cached != null) return cached;

        RepairUserQuota quota = quotas.findById(actorId).orElse(null);
        int maxActive = quota == null ? properties.getDefaultMaxActiveWorkflows() : quota.getMaxActiveWorkflows();
        int dailyLimit = quota == null ? properties.getDefaultDailyTaskLimit() : quota.getDailyTaskLimit();
        int active = runtimes.findById(actorId).map(RepairUserRuntime::getActiveWorkflows).orElse(0);
        int accepted = dailyUsage.findByUserIdAndUsageDate(actorId, LocalDate.now(ZoneOffset.UTC))
                .map(RepairDailyUsage::getAcceptedCount).orElse(0);
        int global = capacities.findById(RepairSystemCapacity.GLOBAL).map(RepairSystemCapacity::getActiveWorkflows).orElse(0);
        long waiting = tasks.countByWorkflowStatusAndStageStatus(RepairWorkflowStatus.RUNNING, RepairStageStatus.WAITING);
        long averageMs = Math.round(executions.findAll().stream().map(RepairStageExecution::getDurationMs)
                .filter(value -> value != null && value > 0).mapToLong(Long::longValue).average().orElse(10_000));
        long parallelism = Math.max(1, properties.getGlobalMaxInflight());
        long eta = waiting == 0 ? 0 : Math.max(1, (waiting * averageMs) / parallelism / 1000);
        Instant reset = LocalDate.now(ZoneOffset.UTC).plusDays(1).atStartOfDay().toInstant(ZoneOffset.UTC);
        RepairAdmissionStatusResponse response = new RepairAdmissionStatusResponse(
                active, maxActive, Math.max(0, maxActive - active), accepted, dailyLimit,
                Math.max(0, dailyLimit - accepted), global, properties.getGlobalMaxInflight(),
                waiting, eta, reset
        );
        cache.put(cacheKey, response, statusCacheTtl);
        return response;
    }

    private String statusCacheKey(UUID actorId) {
        return "repair:capacity:" + actorId;
    }
}
