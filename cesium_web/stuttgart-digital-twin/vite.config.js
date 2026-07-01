import { defineConfig } from 'vite';

// Dev (localhost:5173) serves from "/", exactly like before this project was
// set up for Pages. The production build for GitHub Pages serves from the
// /stuttgart_gis/ subpath. import.meta.env.BASE_URL reflects whichever is active,
// so the relative tileset URLs work in both.
export default defineConfig(({ mode }) => ({
  base: mode === 'production' ? '/stuttgart_gis/' : '/',
}));
