import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './index.css';

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>
  );
  // 标记渲染完成，触发表淡入
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      root.classList.add('ready');
    });
  });
}
