package com.artoria.repoagentbackend.memory;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.UUID;

@Service
public class MemoryRecallRecorder {
    private final MemoryRecallJpaRepository recalls;

    public MemoryRecallRecorder(MemoryRecallJpaRepository recalls) {
        this.recalls = recalls;
    }

    @Transactional
    public void record(UUID taskId, List<MemorySearchHit> hits) {
        hits.forEach(hit -> recalls.insertIfAbsent(
                UUID.randomUUID().toString(), taskId.toString(), hit.memory().id().toString(),
                hit.memory().revision(), hit.score()
        ));
    }
}
