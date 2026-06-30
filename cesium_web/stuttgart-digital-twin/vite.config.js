import { defineConfig } from 'vite';

// GitHub Pages serves this project at https://melibee2024.github.io/stuttgart_gis/,
// so all assets (including the bundled tiles in public/) must resolve from that
// base. import.meta.env.BASE_URL then equals "/stuttgart_gis/" in the build and
// "/" during `npm run dev`, so the relative tileset URLs work in both.
export default defineConfig({
  base: '/stuttgart_gis/',
});
