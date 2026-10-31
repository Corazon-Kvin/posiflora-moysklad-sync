name: Sync Posiflora to MoySklad (Split)

on:
  schedule:
    # 1. Витрина (быстрый скрипт) — каждые 30 минут
    - cron: '*/30 * * * *'
    
    # 2. Инвентарь (средний скрипт) — каждые 2 часа в 15 минут (чтобы не пересекаться с витриной)
    - cron: '15 */2 * * *'
    
    # 3. Санитарная очистка (тяжелый скрипт) — 1 раз в сутки в 03:00 ночи (когда серверы свободны)
    - cron: '0 3 * * *'
    
  workflow_dispatch:
    inputs:
      script_to_run:
        description: 'Какой скрипт запустить вручную?'
        required: true
        default: 'sync_showcase.py'
        type: choice
        options:
          - sync_showcase.py
          - sync_inventory.py
          - sync_cleanup.py

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests Pillow

      - name: Run automated scheduled script
        if: github.event_name == 'schedule'
        env:
          POSIFLORA_USER: ${{ secrets.POSIFLORA_USER }}
          POSIFLORA_PASS: ${{ secrets.POSIFLORA_PASS }}
          POSIFLORA_STORE_ID: ${{ secrets.POSIFLORA_STORE_ID }}
          POSIFLORA_URL: ${{ secrets.POSIFLORA_URL }}
          MOYSKLAD_TOKEN: ${{ secrets.MOYSKLAD_TOKEN }}
          MOYSKLAD_URL: ${{ secrets.MOYSKLAD_URL }}
          TARGET_STORE_ID: ${{ secrets.TARGET_STORE_ID }}
          ORG_ID: ${{ secrets.ORG_ID }}
        run: |
          # Логика выбора скрипта в зависимости от cron-вызова
          if [[ "${{ github.event.schedule }}" == "*/30 * * * *" ]]; then
            python sync_showcase.py
          elif [[ "${{ github.event.schedule }}" == "15 */2 * * *" ]]; then
            python sync_inventory.py
          elif [[ "${{ github.event.schedule }}" == "0 3 * * *" ]]; then
            python sync_cleanup.py
          else
            # Резервный запуск витрины по умолчанию
            python sync_showcase.py
          fi

      - name: Run manual script
        if: github.event_name == 'workflow_dispatch'
        env:
          POSIFLORA_USER: ${{ secrets.POSIFLORA_USER }}
          POSIFLORA_PASS: ${{ secrets.POSIFLORA_PASS }}
          POSIFLORA_STORE_ID: ${{ secrets.POSIFLORA_STORE_ID }}
          POSIFLORA_URL: ${{ secrets.POSIFLORA_URL }}
          MOYSKLAD_TOKEN: ${{ secrets.MOYSKLAD_TOKEN }}
          MOYSKLAD_URL: ${{ secrets.MOYSKLAD_URL }}
          TARGET_STORE_ID: ${{ secrets.TARGET_STORE_ID }}
          ORG_ID: ${{ secrets.ORG_ID }}
        run: |
          python ${{ inputs.script_to_run }}
