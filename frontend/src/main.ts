import './style.css';
import { getConfig } from './config';
import { AssistantApp } from './ui/assistant';

function boot(): void {
  const app = new AssistantApp(getConfig());
  void app.start();
  (window as unknown as { L2D_AI?: AssistantApp }).L2D_AI = app;
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}
