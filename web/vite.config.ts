import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [svelte()],
  resolve: {
    conditions: ['browser']
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts']
  }
});
