<script setup lang="ts">
import type { Component } from 'vue'

defineProps<{
  label: string
  value: string | number
  hint: string
  icon: Component
  tone?: 'default' | 'warning' | 'success' | 'danger'
}>()
</script>

<template>
  <article class="metric-card" :class="`metric-${tone || 'default'}`">
    <div class="metric-icon"><component :is="icon" :size="18" /></div>
    <div>
      <p>{{ label }}</p>
      <strong>{{ value }}</strong>
      <small>{{ hint }}</small>
    </div>
  </article>
</template>
