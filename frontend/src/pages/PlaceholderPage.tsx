import React from 'react';
import { useApp } from '@/context/AppContext';

export function PlaceholderPage({ icon, title, desc }: { icon: string; title: string; desc: string }) {
  const { t } = useApp();
  return (
    <div className="flex flex-col items-center justify-center py-24">
      <div className="text-6xl mb-4">{icon}</div>
      <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">{title}</h2>
      <p className="text-gray-500 dark:text-gray-400 text-center max-w-md">{desc}</p>
      <div className="mt-6 px-4 py-2 bg-yellow-50 dark:bg-yellow-900/20 text-yellow-700 dark:text-yellow-400 rounded-lg text-sm">
        {t('common.developing')}
      </div>
    </div>
  );
}
