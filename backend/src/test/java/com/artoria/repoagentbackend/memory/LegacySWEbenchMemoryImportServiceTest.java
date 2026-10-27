package com.artoria.repoagentbackend.memory;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.ArgumentCaptor;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class LegacySWEbenchMemoryImportServiceTest {
    @TempDir Path tempDir;

    @Test
    void importsVerifiedLegacyFilesAsGlobalMemoriesAndQueuesEmbeddings() throws Exception {
        SemanticMemoryJpaRepository memories = org.mockito.Mockito.mock(SemanticMemoryJpaRepository.class);
        MemoryRevisionJpaRepository revisions = org.mockito.Mockito.mock(MemoryRevisionJpaRepository.class);
        MemoryIndexJobJpaRepository jobs = org.mockito.Mockito.mock(MemoryIndexJobJpaRepository.class);
        MemoryEmbeddingClient embeddings = org.mockito.Mockito.mock(MemoryEmbeddingClient.class);
        when(memories.findByScopeKeyAndFingerprint(any(), any())).thenReturn(Optional.empty());
        when(memories.saveAndFlush(any())).thenAnswer(invocation -> invocation.getArgument(0));
        when(embeddings.isEnabled()).thenReturn(true);
        when(embeddings.profile()).thenReturn("nomic-embed-text-v1");
        when(jobs.findByMemoryIdAndMemoryRevisionAndEmbeddingProfile(any(), anyLong(), any()))
                .thenReturn(Optional.empty());

        Path directory = Files.createDirectories(tempDir.resolve("project/memory"));
        Files.writeString(directory.resolve("repair_recipe_reverse_relation.md"), """
                ---
                name: reverse_relation
                description: Cover reverse Django relations in admin checks
                type: repair_recipe
                provenance: swebench-lite
                ---
                ## Problem Signatures
                Reverse relations pass validation and later crash at render time.
                ## Applicability
                Django admin model checks handling forward and reverse relation kinds.
                ## Repair Strategy
                Extend the existing E109 predicate additively for one_to_many and many_to_many.
                ## Verification Contract
                Run focused admin check tests for reverse FK and reverse M2M fields.
                """);
        Files.writeString(directory.resolve("semantic_insight_guard.md"), """
                ---
                description: Distinguish concrete and semantic integer predicates
                type: semantic_insight
                provenance: swebench-lite
                ---
                A SymPy compound guard must use is_integer for symbolic values and preserve sign boundaries.
                """);
        Files.writeString(directory.resolve("MEMORY.md"), "This index must not be imported.");

        LegacySWEbenchMemoryImportService service = new LegacySWEbenchMemoryImportService(
                memories, revisions, jobs, embeddings
        );

        LegacyMemoryImportResult result = service.importFrom(tempDir);

        assertThat(result).isEqualTo(new LegacyMemoryImportResult(2, 2, 0, 0));
        ArgumentCaptor<SemanticMemory> captured = ArgumentCaptor.forClass(SemanticMemory.class);
        verify(memories, times(2)).saveAndFlush(captured.capture());
        assertThat(captured.getAllValues()).allSatisfy(memory -> {
            assertThat(memory.getScopeType()).isEqualTo(MemoryScopeType.GLOBAL);
            assertThat(memory.getScopeKey()).isEqualTo("HISTORICAL_SWE_BENCH");
            assertThat(memory.getRepositoryId()).isNull();
            assertThat(memory.getRetrievalText()).isNotBlank();
            assertThat(memory.getLanguageName()).isEqualTo("Python");
        });
        assertThat(captured.getAllValues().get(0).getRepairStrategy())
                .contains("Extend the existing E109 predicate");
        verify(revisions, times(2)).save(any(MemoryRevision.class));
        verify(jobs, times(2)).save(any(MemoryIndexJob.class));
    }

    @Test
    void skipsAlreadyImportedFingerprintWithoutCreatingAnotherRevision() throws Exception {
        SemanticMemoryJpaRepository memories = org.mockito.Mockito.mock(SemanticMemoryJpaRepository.class);
        MemoryRevisionJpaRepository revisions = org.mockito.Mockito.mock(MemoryRevisionJpaRepository.class);
        MemoryIndexJobJpaRepository jobs = org.mockito.Mockito.mock(MemoryIndexJobJpaRepository.class);
        MemoryEmbeddingClient embeddings = org.mockito.Mockito.mock(MemoryEmbeddingClient.class);
        when(memories.findByScopeKeyAndFingerprint(any(), any())).thenReturn(Optional.of(org.mockito.Mockito.mock(SemanticMemory.class)));
        Path directory = Files.createDirectories(tempDir.resolve("project/memory"));
        Files.writeString(directory.resolve("semantic_insight_existing.md"), """
                ---
                description: Existing semantic guard memory
                type: semantic_insight
                ---
                Preserve predicate boundaries in a compound guard.
                """);

        LegacyMemoryImportResult result = new LegacySWEbenchMemoryImportService(
                memories, revisions, jobs, embeddings
        ).importFrom(tempDir);

        assertThat(result).isEqualTo(new LegacyMemoryImportResult(1, 0, 1, 0));
        verifyNoInteractions(revisions, jobs);
    }
}
