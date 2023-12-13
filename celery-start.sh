#!/bin/bash

# Start Celery Beat
celery -A your_project_name beat --loglevel=info &

# Start Celery Worker
celery -A your_project_name worker --loglevel=info &

# Start Flower
flower -A your_project_name --port=5555
