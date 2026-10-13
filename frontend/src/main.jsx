import './sentry'; // must be first: initialises Sentry before the app renders
import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';

createRoot(document.getElementById('root')).render(<App />);
