package com.artoria.repoagentbackend.repair;

import org.springframework.stereotype.Component;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;

@Component
public class RepairTaskEventStream {
    private final Map<UUID, CopyOnWriteArrayList<SseEmitter>> subscribers = new ConcurrentHashMap<>();

    public SseEmitter open(UUID taskId, List<RepairTaskEventResponse> replay, long timeoutMillis) {
        SseEmitter emitter = new SseEmitter(timeoutMillis);
        CopyOnWriteArrayList<SseEmitter> taskSubscribers = subscribers.computeIfAbsent(
                taskId, ignored -> new CopyOnWriteArrayList<>()
        );
        taskSubscribers.add(emitter);
        Runnable remove = () -> remove(taskId, emitter);
        emitter.onCompletion(remove);
        emitter.onTimeout(remove);
        emitter.onError(ignored -> remove.run());
        try {
            for (RepairTaskEventResponse event : replay) {
                send(emitter, event);
            }
            emitter.send(SseEmitter.event().name("ready").data(Map.of("taskId", taskId)));
        } catch (IOException error) {
            remove.run();
            emitter.completeWithError(error);
        }
        return emitter;
    }

    public void publish(UUID taskId, RepairTaskEventResponse event) {
        List<SseEmitter> taskSubscribers = subscribers.get(taskId);
        if (taskSubscribers == null) return;
        for (SseEmitter emitter : taskSubscribers) {
            try {
                send(emitter, event);
            } catch (IOException error) {
                remove(taskId, emitter);
                emitter.completeWithError(error);
            }
        }
    }

    private void send(SseEmitter emitter, RepairTaskEventResponse event) throws IOException {
        emitter.send(SseEmitter.event()
                .id(event.id().toString())
                .name("repair-task-event")
                .data(event));
    }

    private void remove(UUID taskId, SseEmitter emitter) {
        List<SseEmitter> taskSubscribers = subscribers.get(taskId);
        if (taskSubscribers == null) return;
        taskSubscribers.remove(emitter);
        if (taskSubscribers.isEmpty()) subscribers.remove(taskId, taskSubscribers);
    }
}

