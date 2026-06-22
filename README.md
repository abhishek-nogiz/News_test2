# COMMAND 
    docker build -t trend-agent-dev .
    
    docker run --rm -t \
        --name trend-agent-dev \
        --env-file .env \
        -e TZ=Asia/Kolkata \
        -p 8000:8000 \
        -v "$(pwd):/app" \
        -w /app \
        trend-agent-dev \
        sh -c "python -m app.scheduler_app 0.0.0.0:8000"