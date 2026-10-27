package com.artoria.repoagentbackend.memory;

import java.util.List;

public record MemoryEmbeddingResult(String model, List<Double> vector) {
}
