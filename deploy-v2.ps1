$ErrorActionPreference = "Stop"

$AWS_ACCOUNT_ID = "338071012734"
$AWS_REGION = "us-east-1"
$REPO_NAME = "shelves-finder"
$ECR_REGISTRY = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
$IMAGE_URI = "${ECR_REGISTRY}/${REPO_NAME}:v2-latest"

Write-Host "🚀 Starting V2 Deployment Process..." -ForegroundColor Cyan

Write-Host "`n🔐 Logging into Amazon ECR..." -ForegroundColor Yellow
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY

Write-Host "`n🔨 Building V2 Docker Image..." -ForegroundColor Yellow
docker build -t ${REPO_NAME}:v2-latest .

Write-Host "`n🏷️ Tagging Image..." -ForegroundColor Yellow
docker tag ${REPO_NAME}:v2-latest $IMAGE_URI

Write-Host "`n☁️ Pushing V2 Image to ECR..." -ForegroundColor Yellow
docker push $IMAGE_URI

Write-Host "`n🔄 Updating EKS V2 Deployment Configuration..." -ForegroundColor Yellow
kubectl apply -f k8s-deploy-v2.yaml

Write-Host "`n🔄 Restarting EKS V2 Deployment..." -ForegroundColor Yellow
kubectl rollout restart deployment shelves-finder-v2-app

Write-Host "`n✅ V2 Deployment successfully triggered! Watching pods spin up..." -ForegroundColor Green
kubectl get pods -l app=shelves-finder-v2 -w
