package com.artoria.repoagentbackend.repair;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * 管理 Worker 的实时在线状态。每个逻辑 Worker 只有当前注册实例可以续期，旧进程的迟到
 * 心跳会被 Lua 脚本拒绝；索引使用 ZSET，读取时无需扫描 Redis keyspace。
 */
@Component
public class WorkerPresenceStore {
    private static final Logger LOGGER = LoggerFactory.getLogger(WorkerPresenceStore.class);
    private static final String INDEX_KEY = "worker:presence:index";

    private static final DefaultRedisScript<Long> REGISTER_SCRIPT = new DefaultRedisScript<>("""
            redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[4])
            redis.call('set', KEYS[2], ARGV[3], 'EX', ARGV[4])
            redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[4])
            redis.call('zadd', KEYS[4], ARGV[5], ARGV[6])
            return 1
            """, Long.class);

    private static final DefaultRedisScript<Long> HEARTBEAT_SCRIPT = new DefaultRedisScript<>("""
            local current = redis.call('get', KEYS[1])
            if not current then return -1 end
            if current ~= ARGV[1] then return 0 end
            local previous = tonumber(redis.call('get', KEYS[3]) or '-1')
            local incoming = tonumber(ARGV[2])
            if incoming < previous then return 0 end
            redis.call('set', KEYS[2], ARGV[3], 'EX', ARGV[4])
            redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[4])
            redis.call('expire', KEYS[1], ARGV[4])
            redis.call('zadd', KEYS[4], ARGV[5], ARGV[6])
            return 1
            """, Long.class);

    private static final DefaultRedisScript<Long> UNREGISTER_SCRIPT = new DefaultRedisScript<>("""
            if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
            redis.call('del', KEYS[1], KEYS[2], KEYS[3])
            redis.call('zrem', KEYS[4], ARGV[2])
            return 1
            """, Long.class);

    private final ObjectProvider<StringRedisTemplate> redis;
    private final ObjectMapper objectMapper;
    private final String prefix;
    private final Duration ttl;

    public WorkerPresenceStore(
            ObjectProvider<StringRedisTemplate> redis,
            ObjectMapper objectMapper,
            @Value("${app.cache.prefix:repoagent:v1}") String prefix,
            @Value("${app.monitoring.worker-presence-ttl-seconds:45}") long ttlSeconds
    ) {
        this.redis = redis;
        this.objectMapper = objectMapper;
        this.prefix = prefix.endsWith(":") ? prefix : prefix + ":";
        this.ttl = Duration.ofSeconds(Math.max(5, ttlSeconds));
    }

    public WorkerPresenceWriteResult register(String workerId, String instanceId, String role) {
        Instant now = Instant.now();
        return write(REGISTER_SCRIPT, new WorkerPresence(
                workerId, instanceId, role, "IDLE", null, null, now, now, 0
        ), 0, false);
    }

    public WorkerPresenceWriteResult heartbeat(
            String workerId,
            String instanceId,
            long sequence,
            String role,
            String status,
            UUID activeTaskId,
            String metadataJson,
            Instant startedAt
    ) {
        Instant now = Instant.now();
        return write(HEARTBEAT_SCRIPT, new WorkerPresence(
                workerId, instanceId, role, status, activeTaskId, metadataJson,
                startedAt == null ? now : startedAt, now, sequence
        ), sequence, true);
    }

    public WorkerPresenceWriteResult unregister(String workerId, String instanceId) {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return WorkerPresenceWriteResult.unavailableResult();
        try {
            Long result = template.execute(
                    UNREGISTER_SCRIPT,
                    List.of(currentKey(workerId), presenceKey(workerId, instanceId), sequenceKey(workerId, instanceId), indexKey()),
                    instanceId, workerId
            );
            return Long.valueOf(1L).equals(result)
                    ? WorkerPresenceWriteResult.acceptedResult()
                    : WorkerPresenceWriteResult.rejectedResult();
        } catch (RuntimeException error) {
            LOGGER.debug("Redis worker presence unregister failed for worker={}", workerId, error);
            return WorkerPresenceWriteResult.unavailableResult();
        }
    }

    public WorkerPresenceSnapshot current() {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return WorkerPresenceSnapshot.unavailable();
        try {
            long now = Instant.now().toEpochMilli();
            template.opsForZSet().removeRangeByScore(indexKey(), 0, now);
            Set<String> workerIds = template.opsForZSet().rangeByScore(indexKey(), now + 1, Double.MAX_VALUE);
            if (workerIds == null || workerIds.isEmpty()) {
                return new WorkerPresenceSnapshot(true, Map.of());
            }

            List<String> ids = List.copyOf(workerIds);
            List<String> instanceIds = template.opsForValue().multiGet(ids.stream().map(this::currentKey).toList());
            if (instanceIds == null || instanceIds.isEmpty()) {
                return new WorkerPresenceSnapshot(true, Map.of());
            }

            List<String> presenceKeys = new ArrayList<>();
            List<String> presenceWorkerIds = new ArrayList<>();
            for (int index = 0; index < ids.size() && index < instanceIds.size(); index++) {
                String instanceId = instanceIds.get(index);
                if (instanceId == null || instanceId.isBlank()) continue;
                presenceWorkerIds.add(ids.get(index));
                presenceKeys.add(presenceKey(ids.get(index), instanceId));
            }
            if (presenceKeys.isEmpty()) return new WorkerPresenceSnapshot(true, Map.of());

            List<String> payloads = template.opsForValue().multiGet(presenceKeys);
            Map<String, WorkerPresence> active = new HashMap<>();
            for (int index = 0; payloads != null && index < payloads.size(); index++) {
                String payload = payloads.get(index);
                if (payload == null) continue;
                try {
                    WorkerPresence presence = objectMapper.readValue(payload, WorkerPresence.class);
                    if (presenceWorkerIds.get(index).equals(presence.workerId())) {
                        active.put(presence.workerId(), presence);
                    }
                } catch (JsonProcessingException ignored) {
                    // 损坏的单条在线记录只影响该 Worker，后续心跳会覆盖它。
                }
            }
            return new WorkerPresenceSnapshot(true, active);
        } catch (RuntimeException error) {
            LOGGER.debug("Redis worker presence query failed", error);
            return WorkerPresenceSnapshot.unavailable();
        }
    }

    private WorkerPresenceWriteResult write(
            DefaultRedisScript<Long> script, WorkerPresence presence, long sequence, boolean canRequireRegistration
    ) {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) return WorkerPresenceWriteResult.unavailableResult();
        try {
            long expiresAt = Instant.now().plus(ttl).toEpochMilli();
            String payload = objectMapper.writeValueAsString(presence);
            Long result = template.execute(
                    script,
                    List.of(
                            currentKey(presence.workerId()),
                            presenceKey(presence.workerId(), presence.instanceId()),
                            sequenceKey(presence.workerId(), presence.instanceId()),
                            indexKey()
                    ),
                    presence.instanceId(),
                    Long.toString(sequence),
                    payload,
                    Long.toString(ttl.toSeconds()),
                    Long.toString(expiresAt),
                    presence.workerId()
            );
            if (Long.valueOf(1L).equals(result)) return WorkerPresenceWriteResult.acceptedResult();
            if (canRequireRegistration && Long.valueOf(-1L).equals(result)) {
                return WorkerPresenceWriteResult.registrationRequiredResult();
            }
            return WorkerPresenceWriteResult.rejectedResult();
        } catch (RuntimeException | JsonProcessingException error) {
            LOGGER.debug("Redis worker presence write failed for worker={}", presence.workerId(), error);
            return WorkerPresenceWriteResult.unavailableResult();
        }
    }

    private String currentKey(String workerId) {
        return fullKey("worker:presence:current:" + workerId);
    }

    private String presenceKey(String workerId, String instanceId) {
        return fullKey("worker:presence:" + workerId + ":" + instanceId);
    }

    private String sequenceKey(String workerId, String instanceId) {
        return fullKey("worker:presence:sequence:" + workerId + ":" + instanceId);
    }

    private String indexKey() {
        return fullKey(INDEX_KEY);
    }

    private String fullKey(String key) {
        return prefix + key;
    }
}
