package com.artoria.repoagentbackend.repair;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.artoria.repoagentbackend.common.ConflictException;
import com.artoria.repoagentbackend.common.NotFoundException;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.UUID;

@Service
public class RepairOutboxService {
    private final RepairOutboxJpaRepository outbox;
    private final ObjectMapper objectMapper;

    public RepairOutboxService(RepairOutboxJpaRepository outbox, ObjectMapper objectMapper) {
        this.outbox = outbox;
        this.objectMapper = objectMapper;
    }

    public void enqueue(RepairTask task) {
        enqueueStage(task, RepairPipelineStage.COORDINATOR);
    }

    /**
     * Stage event names are deterministic per task. A lease recovery can therefore reschedule the
     * same stage without creating duplicate work, while a valid handoff creates the next stage.
     */
    public void enqueueStage(RepairTask task, RepairPipelineStage stage) {
        String payload = serialize(task, stage);
        long sequence = task.getStageSequence();
        String eventType = RepairOutboxEvent.pipelineEventType(stage, sequence);
        RepairOutboxEvent event = outbox.findByAggregateIdAndEventType(
                task.getId(), eventType
        ).orElse(null);
        if (event == null) {
            outbox.save(new RepairOutboxEvent(task.getId(), eventType, payload, sequence));
        } else {
            event.reschedule(payload);
        }
    }

    @Transactional(readOnly = true)
    public Page<RepairOutboxResponse> list(RepairOutboxStatus status, Pageable pageable) {
        Page<RepairOutboxEvent> events = status == null
                ? outbox.findAll(pageable)
                : outbox.findByStatus(status, pageable);
        return events.map(RepairOutboxResponse::from);
    }

    @Transactional
    public RepairOutboxResponse retry(UUID eventId) {
        RepairOutboxEvent event = outbox.findByIdForUpdate(eventId)
                .orElseThrow(() -> new NotFoundException("Repair outbox event not found: " + eventId));
        if (event.getStatus() != RepairOutboxStatus.FAILED
                && event.getStatus() != RepairOutboxStatus.DEAD) {
            throw new ConflictException("Only failed or dead outbox events can be retried");
        }
        event.reschedule(event.getPayloadJson());
        return RepairOutboxResponse.from(event);
    }

    private String serialize(RepairTask task, RepairPipelineStage stage) {
        try {
            return objectMapper.writeValueAsString(new RepairTaskMessage(
                    UUID.randomUUID(), task.getId(), task.getIssue().getId(), task.getBaseSha(), task.getBranchName(),
                    stage, task.getStageSequence()
            ));
        } catch (JsonProcessingException error) {
            throw new IllegalStateException("Cannot serialize repair task outbox payload", error);
        }
    }
}
