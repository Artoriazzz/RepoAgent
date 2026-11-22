package com.artoria.repoagentbackend.repair;

import com.artoria.repoagentbackend.common.RedisJsonCache;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

import java.time.Duration;
import java.util.List;
import java.util.UUID;

@Component
class RepairAgentContextStore {
    private final JdbcTemplate jdbc;
    private final RedisJsonCache cache;
    private final ObjectMapper mapper;
    private final Duration ttl;

    RepairAgentContextStore(JdbcTemplate jdbc, RedisJsonCache cache, ObjectMapper mapper,
                            @Value("${app.repair.agent-context-cache-ttl-seconds:3600}") long ttlSeconds) {
        this.jdbc = jdbc;
        this.cache = cache;
        this.mapper = mapper;
        this.ttl = Duration.ofSeconds(ttlSeconds);
    }

    void archive(RepairTask task, JsonNode snapshot) {
        if (snapshot == null || snapshot.isNull()) return; // Older Workers do not send a snapshot.
        String key = "agent-context:" + task.getId() + ":" + task.getStageSequence() + ":" + task.getLeaseEpoch();
        jdbc.update("""
                INSERT INTO repair_agent_context (context_key, task_id, stage_sequence, pipeline_stage, body)
                VALUES (?, ?, ?, ?, ?)
                """, key, task.getId().toString(), task.getStageSequence(),
                task.getPipelineStage().name(), snapshot.toString());
        // Do not publish an uncommitted snapshot or make a Redis call while holding the task lock.
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                cache.put(key, snapshot, ttl);
            }
        });
    }

    References referencesFor(RepairTask task) {
        if (task.getStageSequence() == 0) return new References(null, null);
        String id = task.getId().toString();
        String own = first(jdbc.queryForList("""
                SELECT context_key FROM repair_agent_context
                WHERE task_id = ? AND pipeline_stage = ? AND stage_sequence < ?
                ORDER BY stage_sequence DESC LIMIT 1
                """, String.class, id, task.getPipelineStage().name(), task.getStageSequence()));
        String incoming = first(jdbc.queryForList("""
                SELECT context_key FROM repair_agent_context WHERE task_id = ? AND stage_sequence = ?
                """, String.class, id, task.getStageSequence() - 1));
        return new References(own, incoming);
    }

    Snapshot read(String key) {
        if (key == null) return null;
        JsonNode body = cache.get(key, JsonNode.class).orElseGet(() -> {
            String json = jdbc.queryForObject("""
                    SELECT body FROM repair_agent_context WHERE context_key = ?
                    """, String.class, key);
            try {
                JsonNode archived = mapper.readTree(json);
                cache.put(key, archived, ttl);
                return archived;
            } catch (JsonProcessingException error) {
                throw new IllegalStateException("Archived agent context is not valid JSON", error);
            }
        });
        return new Snapshot(key, body);
    }

    private static String first(List<String> keys) {
        return keys.isEmpty() ? null : keys.get(0);
    }

    record References(String ownKey, String incomingKey) {}

    record Snapshot(String contextKey, JsonNode body) {}
}
