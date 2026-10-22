# Permissions that used to be declared next to the function URLs. The provider now
# manages them (see lambda.tf). Forget them without deleting the live statements.
removed {
  from = aws_lambda_permission.heartbeat_url
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_lambda_permission.heartbeat_url_invoke
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_lambda_permission.demo_app_url
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_lambda_permission.demo_app_url_invoke
  lifecycle {
    destroy = false
  }
}
