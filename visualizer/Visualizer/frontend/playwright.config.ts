import {defineConfig} from '@playwright/test';
const externalServer = process.env.VISUALIZER_TEST_URL;
export default defineConfig({
  testDir: './tests', timeout: 60_000, expect: {timeout: 15_000}, workers: 1,
  use: {baseURL: externalServer || 'http://127.0.0.1:8000', headless: true, viewport: {width: 1440, height: 1100}},
  webServer: externalServer ? undefined : {command: '..\\.venv\\Scripts\\python.exe -m uvicorn visualizer.app:app --host 127.0.0.1 --port 8000', url: 'http://127.0.0.1:8000/api/health', reuseExistingServer: false, timeout: 60_000},
});
