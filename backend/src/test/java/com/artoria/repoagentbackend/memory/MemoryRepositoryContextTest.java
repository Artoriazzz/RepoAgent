package com.artoria.repoagentbackend.memory;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;
import org.springframework.data.domain.PageRequest;

import java.util.List;
import java.util.Set;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;

@DataJpaTest(properties = {
        "spring.flyway.enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop",
        "spring.datasource.url=jdbc:h2:mem:memory-test;MODE=MySQL;DB_CLOSE_DELAY=-1"
})
class MemoryRepositoryContextTest {
    @Autowired SemanticMemoryJpaRepository memories;
    @Autowired MemoryRevisionJpaRepository revisions;
    @Autowired MemoryIndexJobJpaRepository indexJobs;

    @Test
    void memoryQueriesAreValidAndReturnEmptyResults() {
        assertThat(memories.findRecallCandidates(
                List.of("GLOBAL"), MemoryStatus.ACTIVE, PageRequest.of(0, 10)
        )).isEmpty();
        assertThat(revisions.findMemoryIdsSourcedFromIssue(UUID.randomUUID())).isEmpty();
        assertThat(indexJobs.findDispatchableIds(
                Set.of(MemoryIndexJobStatus.PENDING), java.time.Instant.now(), PageRequest.of(0, 10)
        )).isEmpty();
    }
}
