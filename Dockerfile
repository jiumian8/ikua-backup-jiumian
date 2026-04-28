FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 CD2 proto 文件并编译生成 python 代码
RUN apt-get update && apt-get install -y curl && \
    curl -o clouddrive.proto https://www.clouddrive2.com/api/clouddrive.proto && \
    python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. clouddrive.proto && \
    apt-get remove -y curl && apt-get autoremove -y && apt-get clean

# 拷贝代码
COPY app.py .
COPY templates/ ./templates/

# 暴露 UI 端口
EXPOSE 5000

CMD ["python", "app.py"]