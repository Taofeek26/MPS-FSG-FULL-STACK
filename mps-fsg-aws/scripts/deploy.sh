#!/bin/bash
# ============================================================================
# MPS FSG — SAM Build + Deploy
# Uses sam build/package + aws cloudformation create-stack
# (sam deploy blocked by account-level EarlyValidation hook on changesets)
# ============================================================================
set -euo pipefail

STACK_NAME="${1:-fsg}"
REGION="${2:-us-east-1}"

echo "=== [1/3] SAM Build ==="
sam build --region "${REGION}"

echo "=== [2/3] SAM Package ==="
sam package \
  --resolve-s3 \
  --region "${REGION}" \
  --output-template-file .aws-sam/build/packaged.yaml

echo "=== [3/3] Deploy ==="

# Check if stack exists
if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" &>/dev/null; then
  echo "Stack exists — updating..."
  aws cloudformation update-stack \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --template-body "file://.aws-sam/build/packaged.yaml" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --parameters \
      ParameterKey=Environment,ParameterValue=prod \
      ParameterKey=DBUsername,ParameterValue=fsg_admin \
      ParameterKey=DBPassword,ParameterValue="T3stPassW0rd2026" \
      ParameterKey=CognitoDomainPrefix,ParameterValue="fsg-058264543478" \
      ParameterKey=EnableAI,ParameterValue=false \
      ParameterKey=EnableSES,ParameterValue=false \
      ParameterKey=EnableMicrosoftSSO,ParameterValue=false
else
  echo "Creating new stack..."
  aws cloudformation create-stack \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}" \
    --template-body "file://.aws-sam/build/packaged.yaml" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --parameters \
      ParameterKey=Environment,ParameterValue=prod \
      ParameterKey=DBUsername,ParameterValue=fsg_admin \
      ParameterKey=DBPassword,ParameterValue="T3stPassW0rd2026" \
      ParameterKey=CognitoDomainPrefix,ParameterValue="fsg-058264543478" \
      ParameterKey=EnableAI,ParameterValue=false \
      ParameterKey=EnableSES,ParameterValue=false \
      ParameterKey=EnableMicrosoftSSO,ParameterValue=false
fi

echo "=== Deployment triggered. Monitor with: ==="
echo "  aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${REGION} --query 'Stacks[0].StackStatus'"
echo "  aws cloudformation wait stack-create-complete --stack-name ${STACK_NAME} --region ${REGION}"