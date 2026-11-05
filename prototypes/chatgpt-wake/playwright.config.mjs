import {defineConfig} from '/home/marco/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/test.mjs';
export default defineConfig({testDir:'./playwright', workers:1, fullyParallel:false, timeout:30000, reporter:'list', use:{trace:'retain-on-failure'}});
