variable "IMAGE" {
  default = "restricted-content-downloader:latest"
}

target "default" {
  context    = "."
  dockerfile = "Dockerfile"
  platforms  = ["linux/amd64", "linux/arm64"]
  tags       = [IMAGE]
}
