#!/bin/bash
echo "Pulling latest OSINT tool code..."
git pull origin main

echo "Rebuilding and deploying Docker container..."
docker-compose up -d --build

echo "Deployment complete!"

