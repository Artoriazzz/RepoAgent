package com.artoria.repoagentbackend.memory;

import java.util.List;

/** 版本化记忆向量的缓存值；模型和维度用于拒绝错误配置下的混用。 */
public record MemoryVectorValue(String model, int dimensions, List<Double> vector) {
    public MemoryVectorValue {
        vector = vector == null ? List.of() : List.copyOf(vector);
    }

    boolean isCompatibleWith(String expectedModel, int expectedDimensions) {
        return model != null && model.equals(expectedModel)
                && dimensions == expectedDimensions && vector.size() == expectedDimensions;
    }
}
