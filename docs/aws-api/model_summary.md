# lambda-microvms service model 2025-09-09 — protocol=rest-json endpointPrefix=lambda serviceId=Lambda Microvms


## CreateMicrovmAuthToken  `POST /2025-09-09/microvms/{microvmIdentifier}/auth-token`
> <p>Creates an authentication token for accessing a running MicroVM. The token grants access to the specified ports on the MicroVM endpoint.</p>
**Input** CreateMicrovmAuthTokenRequest:
- microvmIdentifier*: string (min=1, max=256) [uri:microvmIdentifier]
- expirationInMinutes*: integer (min=1)
- allowedPorts*: ListOfPortSpecification (list)
  - [list of PortSpecification]
    - port: integer (min=1, max=65535)
    - range: PortRange (structure)
      - startPort*: integer (min=1, max=65535)
      - endPort*: integer (min=1, max=65535)
    - allPorts: Unit (structure)
**Output** CreateMicrovmAuthTokenResponse:
- authToken*: TokenParts (map)
  - map<AuthTokenKey,AuthTokenValue>
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## CreateMicrovmImage  `POST /2025-09-09/microvm-images`
> <p>Creates a MicroVM image from the specified code artifact and base image. The build is asynchronous — the image transitions from CREATING to CREATED on success, or CREATE_FAILED on failure. Use GetMicrovmImage to poll for completion.</p>
**Input** CreateMicrovmImageRequest:
- baseImageArn*: string (min=1, max=2048, pattern=[^\s]+)
- baseImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- buildRoleArn*: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- description: string
- codeArtifact*: CodeArtifact (structure)
  - uri: string (min=1, max=2048, pattern=[^\s]+)
- logging: Logging (structure)
  - disabled: LoggingDisabled (structure)
  - cloudWatch: CloudWatchLogging (structure)
    - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
    - logStream: string (min=1, max=512, pattern=[^:*]*)
- egressNetworkConnectors: CreateMicrovmImageRequestEgressNetworkConnectorsList (list)
  - [list of string]
- cpuConfigurations: CpuConfigurationList (list)
  - [list of CpuConfiguration]
    - architecture*: string enum=['ARM_64']
- resources: ResourcesList (list)
  - [list of Resources]
    - minimumMemoryInMiB*: integer
- additionalOsCapabilities: CapabilityList (list)
  - [list of string enum=['ALL']]
- hooks: Hooks (structure)
  - port: integer (min=1, max=65535)
  - microvmHooks: MicrovmHooks (structure)
    - run: string enum=['DISABLED', 'ENABLED']
    - runTimeoutInSeconds: integer (min=1, max=60)
    - resume: string enum=['DISABLED', 'ENABLED']
    - resumeTimeoutInSeconds: integer (min=1, max=60)
    - suspend: string enum=['DISABLED', 'ENABLED']
    - suspendTimeoutInSeconds: integer (min=1, max=60)
    - terminate: string enum=['DISABLED', 'ENABLED']
    - terminateTimeoutInSeconds: integer (min=1, max=60)
  - microvmImageHooks: MicrovmImageHooks (structure)
    - ready: string enum=['DISABLED', 'ENABLED']
    - readyTimeoutInSeconds: integer (min=1, max=3600)
    - validate: string enum=['DISABLED', 'ENABLED']
    - validateTimeoutInSeconds: integer (min=1, max=3600)
- environmentVariables: EnvironmentVariableMap (map)
  - map<EnvironmentVariableKey,EnvironmentVariableValue>
- name*: string (min=1, max=64, pattern=[a-zA-Z0-9-_]+)
- tags: Tags (map)
  - map<TagKey,TagValue>
- clientToken: string (min=1, max=128)
**Output** CreateMicrovmImageResponse:
- imageArn*: string (min=1, max=2048, pattern=[^\s]+)
- name*: string (min=1, max=64, pattern=[a-zA-Z0-9-_]+)
- state*: string enum=['CREATING', 'CREATED', 'CREATE_FAILED', 'UPDATING', 'UPDATED', 'UPDATE_FAILED', 'DELETING', 'DELETE_FAILED', 'DELETED']
- latestActiveImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- latestFailedImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- createdAt*: timestamp
- baseImageArn*: string (min=1, max=2048, pattern=[^\s]+)
- baseImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- buildRoleArn*: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- description: string
- codeArtifact*: CodeArtifact (structure)
  - uri: string (min=1, max=2048, pattern=[^\s]+)
- logging: Logging (structure)
  - disabled: LoggingDisabled (structure)
  - cloudWatch: CloudWatchLogging (structure)
    - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
    - logStream: string (min=1, max=512, pattern=[^:*]*)
- egressNetworkConnectors: CreateMicrovmImageResponseEgressNetworkConnectorsList (list)
  - [list of string]
- cpuConfigurations: CpuConfigurationList (list)
  - [list of CpuConfiguration]
    - architecture*: string enum=['ARM_64']
- resources: ResourcesList (list)
  - [list of Resources]
    - minimumMemoryInMiB*: integer
- additionalOsCapabilities: CapabilityList (list)
  - [list of string enum=['ALL']]
- hooks: Hooks (structure)
  - port: integer (min=1, max=65535)
  - microvmHooks: MicrovmHooks (structure)
    - run: string enum=['DISABLED', 'ENABLED']
    - runTimeoutInSeconds: integer (min=1, max=60)
    - resume: string enum=['DISABLED', 'ENABLED']
    - resumeTimeoutInSeconds: integer (min=1, max=60)
    - suspend: string enum=['DISABLED', 'ENABLED']
    - suspendTimeoutInSeconds: integer (min=1, max=60)
    - terminate: string enum=['DISABLED', 'ENABLED']
    - terminateTimeoutInSeconds: integer (min=1, max=60)
  - microvmImageHooks: MicrovmImageHooks (structure)
    - ready: string enum=['DISABLED', 'ENABLED']
    - readyTimeoutInSeconds: integer (min=1, max=3600)
    - validate: string enum=['DISABLED', 'ENABLED']
    - validateTimeoutInSeconds: integer (min=1, max=3600)
- environmentVariables: EnvironmentVariableMap (map)
  - map<EnvironmentVariableKey,EnvironmentVariableValue>
- tags: Tags (map)
  - map<TagKey,TagValue>
- updatedAt: timestamp
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException', 'ServiceQuotaExceededException']

## CreateMicrovmShellAuthToken  `POST /2025-09-09/microvms/{microvmIdentifier}/shell-auth-token`
> <p>Creates a shell authentication token for interactive shell access to a running MicroVM. The MicroVM must have been run with the SHELL_INGRESS network connector attached.</p>
**Input** CreateMicrovmShellAuthTokenRequest:
- microvmIdentifier*: string (min=1, max=256) [uri:microvmIdentifier]
- expirationInMinutes*: integer (min=1)
**Output** CreateMicrovmShellAuthTokenResponse:
- authToken*: TokenParts (map)
  - map<AuthTokenKey,AuthTokenValue>
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## DeleteMicrovmImage  `DELETE /2025-09-09/microvm-images/{imageIdentifier}`
> <p>Deletes a MicroVM image. This operation is idempotent; deleting an image that has already been deleted succeeds without error.</p>
**Input** DeleteMicrovmImageInput:
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
**Output** DeleteMicrovmImageOutput:
- imageIdentifier*: string (min=1, max=256)
- state*: string enum=['CREATING', 'CREATED', 'CREATE_FAILED', 'UPDATING', 'UPDATED', 'UPDATE_FAILED', 'DELETING', 'DELETE_FAILED', 'DELETED']
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException']

## DeleteMicrovmImageVersion  `DELETE /2025-09-09/microvm-images/{imageIdentifier}/versions/{imageVersion}`
> <p>Deletes a specific version of a MicroVM image. This operation is idempotent; deleting a version that has already been deleted succeeds without error.</p>
**Input** DeleteMicrovmImageVersionInput:
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+) [uri:imageVersion]
**Output** DeleteMicrovmImageVersionOutput:
- imageIdentifier*: string (min=1, max=256)
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
- state*: string enum=['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED', 'DELETING', 'DELETED', 'DELETE_FAILED']
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException']

## GetMicrovm  `GET /2025-09-09/microvms/{microvmIdentifier}`
> <p>Retrieves the details of a specific MicroVM, including its state, endpoint, image information, and configuration. The state field is eventually consistent — determine readiness by connecting to the endpoint.</p>
**Input** GetMicrovmRequest:
- microvmIdentifier*: string (min=1, max=256) [uri:microvmIdentifier]
**Output** GetMicrovmResponse:
- microvmId*: string (min=1, max=256)
- state*: string enum=['PENDING', 'RUNNING', 'SUSPENDING', 'SUSPENDED', 'TERMINATING', 'TERMINATED']
- endpoint*: string (min=1, max=2048)
- imageArn*: string (min=20, max=2048)
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
- executionRoleArn: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- idlePolicy: IdlePolicy (structure)
  - maxIdleDurationSeconds*: integer (min=60)
  - suspendedDurationSeconds*: integer (min=0)
  - autoResumeEnabled*: boolean
- maximumDurationInSeconds*: integer
- startedAt*: timestamp
- terminatedAt: timestamp
- stateReason: string (min=1, max=2048, pattern=[^\s]+)
- ingressNetworkConnectors: NetworkConnectorList (list)
  - [list of string]
- egressNetworkConnectors: NetworkConnectorList (list)
  - [list of string]
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## GetMicrovmImage  `GET /2025-09-09/microvm-images/{imageIdentifier}`
> <p>Retrieves the details of a MicroVM image, including its state, versions, and configuration.</p>
**Input** GetMicrovmImageInput:
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
**Output** GetMicrovmImageOutput:
- imageArn*: string (min=1, max=2048, pattern=[^\s]+)
- name*: string (min=1, max=64, pattern=[a-zA-Z0-9-_]+)
- state*: string enum=['CREATING', 'CREATED', 'CREATE_FAILED', 'UPDATING', 'UPDATED', 'UPDATE_FAILED', 'DELETING', 'DELETE_FAILED', 'DELETED']
- latestActiveImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- latestFailedImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- createdAt*: timestamp
- tags: Tags (map)
  - map<TagKey,TagValue>
- updatedAt: timestamp
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## GetMicrovmImageBuild  `GET /2025-09-09/microvm-images/{imageIdentifier}/versions/{imageVersion}/builds/{buildId}`
> <p>Retrieves the details of a specific MicroVM image build, including its state, target architecture, and snapshot information.</p>
**Input** GetMicrovmImageBuildInput:
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+) [uri:imageVersion]
- buildId*: string (min=1, max=2048, pattern=[^\s]+) [uri:buildId]
**Output** GetMicrovmImageBuildOutput:
- imageArn*: string (min=1, max=2048, pattern=[^\s]+)
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
- buildId*: string (min=1, max=2048, pattern=[^\s]+)
- buildState*: string enum=['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED']
- architecture*: string enum=['ARM_64']
- chipset*: string enum=['GRAVITON']
- chipsetGeneration*: string (min=1, max=2048, pattern=[^\s]+)
- stateReason: string
- createdAt*: timestamp
- snapshotBuild: SnapshotBuild (structure)
  - memorySnapshotSizeInBytes: long
  - codeInstallSizeInBytes: long
  - diskSnapshotSizeInBytes: long
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## GetMicrovmImageVersion  `GET /2025-09-09/microvm-images/{imageIdentifier}/versions/{imageVersion}`
> <p>Retrieves the details of a specific version of a MicroVM image, including its configuration, state, and build information.</p>
**Input** GetMicrovmImageVersionInput:
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+) [uri:imageVersion]
**Output** GetMicrovmImageVersionOutput:
- baseImageArn*: string (min=1, max=2048, pattern=[^\s]+)
- baseImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- buildRoleArn*: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- description: string
- codeArtifact*: CodeArtifact (structure)
  - uri: string (min=1, max=2048, pattern=[^\s]+)
- logging: Logging (structure)
  - disabled: LoggingDisabled (structure)
  - cloudWatch: CloudWatchLogging (structure)
    - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
    - logStream: string (min=1, max=512, pattern=[^:*]*)
- egressNetworkConnectors: GetMicrovmImageVersionOutputEgressNetworkConnectorsList (list)
  - [list of string]
- cpuConfigurations: CpuConfigurationList (list)
  - [list of CpuConfiguration]
    - architecture*: string enum=['ARM_64']
- resources: ResourcesList (list)
  - [list of Resources]
    - minimumMemoryInMiB*: integer
- additionalOsCapabilities: CapabilityList (list)
  - [list of string enum=['ALL']]
- hooks: Hooks (structure)
  - port: integer (min=1, max=65535)
  - microvmHooks: MicrovmHooks (structure)
    - run: string enum=['DISABLED', 'ENABLED']
    - runTimeoutInSeconds: integer (min=1, max=60)
    - resume: string enum=['DISABLED', 'ENABLED']
    - resumeTimeoutInSeconds: integer (min=1, max=60)
    - suspend: string enum=['DISABLED', 'ENABLED']
    - suspendTimeoutInSeconds: integer (min=1, max=60)
    - terminate: string enum=['DISABLED', 'ENABLED']
    - terminateTimeoutInSeconds: integer (min=1, max=60)
  - microvmImageHooks: MicrovmImageHooks (structure)
    - ready: string enum=['DISABLED', 'ENABLED']
    - readyTimeoutInSeconds: integer (min=1, max=3600)
    - validate: string enum=['DISABLED', 'ENABLED']
    - validateTimeoutInSeconds: integer (min=1, max=3600)
- environmentVariables: EnvironmentVariableMap (map)
  - map<EnvironmentVariableKey,EnvironmentVariableValue>
- imageArn*: string (min=1, max=2048, pattern=[^\s]+)
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
- state*: string enum=['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED', 'DELETING', 'DELETED', 'DELETE_FAILED']
- status*: string enum=['ACTIVE', 'INACTIVE']
- createdAt*: timestamp
- updatedAt: timestamp
- stateReason: string
- tags: Tags (map)
  - map<TagKey,TagValue>
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## ListManagedMicrovmImageVersions  `GET /2025-09-09/managed-microvm-images/{imageIdentifier}/versions`
> <p>Lists versions of a managed MicroVM image. We recommend using pagination to ensure that the operation returns quickly and successfully.</p>
**Input** ListManagedMicrovmImageVersionsInput:
- maxResults: integer (min=1, max=50) [querystring:maxResults]
- nextToken: string [querystring:nextToken]
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
**Output** ListManagedMicrovmImageVersionsOutput:
- nextToken: string
- items*: ManagedMicrovmImageVersionList (list)
  - [list of ManagedMicrovmImageVersion]
    - imageArn*: string (min=1, max=2048, pattern=[^\s]+)
    - imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
    - createdAt*: timestamp
    - updatedAt: timestamp
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## ListManagedMicrovmImages  `GET /2025-09-09/managed-microvm-images`
> <p>Lists AWS managed MicroVM images available for use as base images. We recommend using pagination to ensure that the operation returns quickly and successfully.</p>
**Input** ListManagedMicrovmImagesInput:
- maxResults: integer (min=1, max=50) [querystring:maxResults]
- nextToken: string [querystring:nextToken]
**Output** ListManagedMicrovmImagesOutput:
- nextToken: string
- items*: ManagedMicrovmImageSummaryList (list)
  - [list of ManagedMicrovmImageSummary]
    - imageArn*: string (min=1, max=2048, pattern=[^\s]+)
    - createdAt*: timestamp
    - updatedAt: timestamp
Errors: ['InternalServerException', 'AccessDeniedException', 'ThrottlingException', 'ValidationException']

## ListMicrovmImageBuilds  `GET /2025-09-09/microvm-images/{imageIdentifier}/versions/{imageVersion}/builds`
> <p>Lists builds for a MicroVM image version with optional filtering by architecture and chipset. We recommend using pagination to ensure that the operation returns quickly and successfully.</p>
**Input** ListMicrovmImageBuildsInput:
- maxResults: integer (min=1, max=50) [querystring:maxResults]
- nextToken: string [querystring:nextToken]
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+) [uri:imageVersion]
- architecture: string enum=['ARM_64'] [querystring:architecture]
- chipset: string enum=['GRAVITON'] [querystring:chipset]
- chipsetGeneration: string (min=1, max=2048, pattern=[^\s]+) [querystring:chipsetGeneration]
**Output** ListMicrovmImageBuildsOutput:
- nextToken: string
- items*: MicrovmImageBuildSummaries (list)
  - [list of MicrovmImageBuildSummary]
    - imageArn*: string (min=1, max=2048, pattern=[^\s]+)
    - imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
    - buildId*: string (min=1, max=2048, pattern=[^\s]+)
    - buildState*: string enum=['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED']
    - architecture*: string enum=['ARM_64']
    - chipset*: string enum=['GRAVITON']
    - chipsetGeneration*: string (min=1, max=2048, pattern=[^\s]+)
    - stateReason: string
    - createdAt*: timestamp
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## ListMicrovmImageVersions  `GET /2025-09-09/microvm-images/{imageIdentifier}/versions`
> <p>Lists versions of a MicroVM image. We recommend using pagination to ensure that the operation returns quickly and successfully.</p>
**Input** ListMicrovmImageVersionsInput:
- maxResults: integer (min=1, max=50) [querystring:maxResults]
- nextToken: string [querystring:nextToken]
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
**Output** ListMicrovmImageVersionsOutput:
- nextToken: string
- items*: MicrovmImageVersionSummaryList (list)
  - [list of MicrovmImageVersionSummary]
    - baseImageArn*: string (min=1, max=2048, pattern=[^\s]+)
    - baseImageVersion: string (min=1, max=2048, pattern=[^\s]+)
    - buildRoleArn*: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
    - description: string
    - codeArtifact*: CodeArtifact (structure)
      - uri: string (min=1, max=2048, pattern=[^\s]+)
    - logging: Logging (structure)
      - disabled: LoggingDisabled (structure)
      - cloudWatch: CloudWatchLogging (structure)
        - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
        - logStream: string (min=1, max=512, pattern=[^:*]*)
    - egressNetworkConnectors: MicrovmImageVersionSummaryEgressNetworkConnectorsList (list)
      - [list of string]
    - cpuConfigurations: CpuConfigurationList (list)
      - [list of CpuConfiguration]
        - architecture*: string enum=['ARM_64']
    - resources: ResourcesList (list)
      - [list of Resources]
        - minimumMemoryInMiB*: integer
    - additionalOsCapabilities: CapabilityList (list)
      - [list of string enum=['ALL']]
    - hooks: Hooks (structure)
      - port: integer (min=1, max=65535)
      - microvmHooks: MicrovmHooks (structure)
        - run: string enum=['DISABLED', 'ENABLED']
        - runTimeoutInSeconds: integer (min=1, max=60)
        - resume: string enum=['DISABLED', 'ENABLED']
        - resumeTimeoutInSeconds: integer (min=1, max=60)
        - suspend: string enum=['DISABLED', 'ENABLED']
        - suspendTimeoutInSeconds: integer (min=1, max=60)
        - terminate: string enum=['DISABLED', 'ENABLED']
        - terminateTimeoutInSeconds: integer (min=1, max=60)
      - microvmImageHooks: MicrovmImageHooks (structure)
        - ready: string enum=['DISABLED', 'ENABLED']
        - readyTimeoutInSeconds: integer (min=1, max=3600)
        - validate: string enum=['DISABLED', 'ENABLED']
        - validateTimeoutInSeconds: integer (min=1, max=3600)
    - environmentVariables: EnvironmentVariableMap (map)
      - map<EnvironmentVariableKey,EnvironmentVariableValue>
    - imageArn*: string (min=1, max=2048, pattern=[^\s]+)
    - imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
    - state*: string enum=['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED', 'DELETING', 'DELETED', 'DELETE_FAILED']
    - status*: string enum=['ACTIVE', 'INACTIVE']
    - createdAt*: timestamp
    - updatedAt: timestamp
    - stateReason: string
    - tags: Tags (map)
      - map<TagKey,TagValue>
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## ListMicrovmImages  `GET /2025-09-09/microvm-images`
> <p>Lists MicroVM images in the account with optional name filtering. We recommend using pagination to ensure that the operation returns quickly and successfully.</p>
**Input** ListMicrovmImagesRequest:
- maxResults: integer (min=1, max=50) [querystring:maxResults]
- nextToken: string [querystring:nextToken]
- nameFilter: string (min=1, max=2048, pattern=[^\s]+) [querystring:nameFilter]
**Output** ListMicrovmImagesResponse:
- nextToken: string
- items*: MicrovmImageSummaries (list)
  - [list of MicrovmImageSummary]
    - imageArn*: string (min=1, max=2048, pattern=[^\s]+)
    - name*: string (min=1, max=64, pattern=[a-zA-Z0-9-_]+)
    - state*: string enum=['CREATING', 'CREATED', 'CREATE_FAILED', 'UPDATING', 'UPDATED', 'UPDATE_FAILED', 'DELETING', 'DELETE_FAILED', 'DELETED']
    - latestActiveImageVersion: string (min=1, max=2048, pattern=[^\s]+)
    - latestFailedImageVersion: string (min=1, max=2048, pattern=[^\s]+)
    - createdAt*: timestamp
Errors: ['InternalServerException', 'AccessDeniedException', 'ThrottlingException', 'ValidationException']

## ListMicrovms  `GET /2025-09-09/microvms`
> <p>Lists MicroVMs in the account with optional filtering by image and version. We recommend using pagination to ensure that the operation returns quickly and successfully.</p>
**Input** ListMicrovmsRequest:
- maxResults: integer (min=1, max=50) [querystring:maxResults]
- nextToken: string [querystring:nextToken]
- imageIdentifier: string (min=1, max=256) [querystring:imageIdentifier]
- imageVersion: string [querystring:imageVersion]
**Output** ListMicrovmsResponse:
- nextToken: string
- items*: MicrovmItemList (list)
  - [list of MicrovmItem]
    - microvmId*: string (min=1, max=256)
    - state*: string enum=['PENDING', 'RUNNING', 'SUSPENDING', 'SUSPENDED', 'TERMINATING', 'TERMINATED']
    - imageArn*: string (min=20, max=2048)
    - imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
    - startedAt*: timestamp
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ValidationException']

## ListTags  `GET /2017-03-31/tags/{Resource}`
> <p>Lists the tags associated with a Lambda MicroVM resource.</p>
**Input** ListTagsRequest:
- Resource*: string (min=1, max=10000, pattern=arn:(aws[a-zA-Z-]*):lambda:(eusc-)?[a-z]{2}((-gov)|(-iso([a-z]?)))?-[a-z]+-\d{1}:\d{12}:(function:[a-zA-Z0-9-_]+(:(\$LATEST|[a-zA-Z0-9-_]+))?|layer:([a-zA-Z0-9-_]+)|code-signing-config:csc-[a-z0-9]{17}|event-source-mapping:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|(capacity-provider|network-connector|microvm-image):[a-zA-Z0-9-_]+)) [uri:Resource]
**Output** ListTagsResponse:
- Tags: Tags (map)
  - map<TagKey,TagValue>
Errors: ['TooManyRequestsException', 'ResourceNotFoundException', 'ServiceException', 'InvalidParameterValueException']

## ResumeMicrovm  `POST /2025-09-09/microvms/{microvmIdentifier}/resume`
> <p>Resumes a suspended MicroVM, restoring it to RUNNING state with all state intact. The MicroVM must be in SUSPENDED state.</p>
**Input** ResumeMicrovmRequest:
- microvmIdentifier*: string (min=1, max=256) [uri:microvmIdentifier]
**Output** ResumeMicrovmResponse:
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException']

## RunMicrovm  `POST /2025-09-09/microvms`
> <p>Runs a new MicroVM from the specified image. The MicroVM starts in PENDING state and transitions to RUNNING once provisioning completes. To connect, generate an authentication token using CreateMicrovmAuthToken.</p>
**Input** RunMicrovmRequest:
- ingressNetworkConnectors: NetworkConnectorList (list)
  - [list of string]
- egressNetworkConnectors: NetworkConnectorList (list)
  - [list of string]
- imageIdentifier*: string (min=1, max=256)
- imageVersion: string (min=1, max=2048, pattern=[^\s]+)
- executionRoleArn: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- idlePolicy: IdlePolicy (structure)
  - maxIdleDurationSeconds*: integer (min=60)
  - suspendedDurationSeconds*: integer (min=0)
  - autoResumeEnabled*: boolean
- logging: Logging (structure)
  - disabled: LoggingDisabled (structure)
  - cloudWatch: CloudWatchLogging (structure)
    - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
    - logStream: string (min=1, max=512, pattern=[^:*]*)
- runHookPayload: string (min=0, max=4096)
- maximumDurationInSeconds: integer (min=1, max=28800)
- clientToken: string (min=1, max=128)
**Output** RunMicrovmResponse:
- microvmId*: string (min=1, max=256)
- state*: string enum=['PENDING', 'RUNNING', 'SUSPENDING', 'SUSPENDED', 'TERMINATING', 'TERMINATED']
- endpoint*: string (min=1, max=2048)
- imageArn*: string (min=20, max=2048)
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
- executionRoleArn: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- idlePolicy: IdlePolicy (structure)
  - maxIdleDurationSeconds*: integer (min=60)
  - suspendedDurationSeconds*: integer (min=0)
  - autoResumeEnabled*: boolean
- maximumDurationInSeconds*: integer
- startedAt*: timestamp
- terminatedAt: timestamp
- stateReason: string (min=1, max=2048, pattern=[^\s]+)
- ingressNetworkConnectors: NetworkConnectorList (list)
  - [list of string]
- egressNetworkConnectors: NetworkConnectorList (list)
  - [list of string]
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException', 'ServiceQuotaExceededException']

## SuspendMicrovm  `POST /2025-09-09/microvms/{microvmIdentifier}/suspend`
> <p>Suspends a running MicroVM, preserving its full memory and disk state. The MicroVM transitions through SUSPENDING to SUSPENDED. To restore, call ResumeMicrovm or send traffic to the endpoint if autoResumeEnabled is true.</p>
**Input** SuspendMicrovmRequest:
- microvmIdentifier*: string (min=1, max=256) [uri:microvmIdentifier]
**Output** SuspendMicrovmResponse:
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException']

## TagResource  `POST /2017-03-31/tags/{Resource}`
> <p>Adds tags to a Lambda MicroVM resource.</p>
**Input** TagResourceRequest:
- Resource*: string (min=1, max=10000, pattern=arn:(aws[a-zA-Z-]*):lambda:(eusc-)?[a-z]{2}((-gov)|(-iso([a-z]?)))?-[a-z]+-\d{1}:\d{12}:(function:[a-zA-Z0-9-_]+(:(\$LATEST|[a-zA-Z0-9-_]+))?|layer:([a-zA-Z0-9-_]+)|code-signing-config:csc-[a-z0-9]{17}|event-source-mapping:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|(capacity-provider|network-connector|microvm-image):[a-zA-Z0-9-_]+)) [uri:Resource]
- Tags*: Tags (map)
  - map<TagKey,TagValue>
Errors: ['TooManyRequestsException', 'ResourceNotFoundException', 'ServiceException', 'InvalidParameterValueException', 'ResourceConflictException']

## TerminateMicrovm  `DELETE /2025-09-09/microvms/{microvmIdentifier}`
> <p>Terminates a MicroVM. This operation is idempotent; terminating a MicroVM that has already been terminated succeeds without error.</p>
**Input** TerminateMicrovmRequest:
- microvmIdentifier*: string (min=1, max=256) [uri:microvmIdentifier]
**Output** TerminateMicrovmResponse:
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException']

## UntagResource  `DELETE /2017-03-31/tags/{Resource}`
> <p>Removes tags from a Lambda MicroVM resource.</p>
**Input** UntagResourceRequest:
- Resource*: string (min=1, max=10000, pattern=arn:(aws[a-zA-Z-]*):lambda:(eusc-)?[a-z]{2}((-gov)|(-iso([a-z]?)))?-[a-z]+-\d{1}:\d{12}:(function:[a-zA-Z0-9-_]+(:(\$LATEST|[a-zA-Z0-9-_]+))?|layer:([a-zA-Z0-9-_]+)|code-signing-config:csc-[a-z0-9]{17}|event-source-mapping:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|(capacity-provider|network-connector|microvm-image):[a-zA-Z0-9-_]+)) [uri:Resource]
- TagKeys*: TagKeyList (list) [querystring:tagKeys]
  - [list of string]
Errors: ['TooManyRequestsException', 'ResourceNotFoundException', 'ServiceException', 'InvalidParameterValueException', 'ResourceConflictException']

## UpdateMicrovmImage  `PUT /2025-09-09/microvm-images/{imageIdentifier}`
> <p>Updates the configuration of a MicroVM image and triggers a new version build. This operation uses PUT semantics — all required fields (codeArtifact, baseImageArn, buildRoleArn) must be provided with every request.</p>
**Input** UpdateMicrovmImageRequest:
- baseImageArn*: string (min=1, max=2048, pattern=[^\s]+)
- baseImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- buildRoleArn*: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- description: string
- codeArtifact*: CodeArtifact (structure)
  - uri: string (min=1, max=2048, pattern=[^\s]+)
- logging: Logging (structure)
  - disabled: LoggingDisabled (structure)
  - cloudWatch: CloudWatchLogging (structure)
    - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
    - logStream: string (min=1, max=512, pattern=[^:*]*)
- egressNetworkConnectors: UpdateMicrovmImageRequestEgressNetworkConnectorsList (list)
  - [list of string]
- cpuConfigurations: CpuConfigurationList (list)
  - [list of CpuConfiguration]
    - architecture*: string enum=['ARM_64']
- resources: ResourcesList (list)
  - [list of Resources]
    - minimumMemoryInMiB*: integer
- additionalOsCapabilities: CapabilityList (list)
  - [list of string enum=['ALL']]
- hooks: Hooks (structure)
  - port: integer (min=1, max=65535)
  - microvmHooks: MicrovmHooks (structure)
    - run: string enum=['DISABLED', 'ENABLED']
    - runTimeoutInSeconds: integer (min=1, max=60)
    - resume: string enum=['DISABLED', 'ENABLED']
    - resumeTimeoutInSeconds: integer (min=1, max=60)
    - suspend: string enum=['DISABLED', 'ENABLED']
    - suspendTimeoutInSeconds: integer (min=1, max=60)
    - terminate: string enum=['DISABLED', 'ENABLED']
    - terminateTimeoutInSeconds: integer (min=1, max=60)
  - microvmImageHooks: MicrovmImageHooks (structure)
    - ready: string enum=['DISABLED', 'ENABLED']
    - readyTimeoutInSeconds: integer (min=1, max=3600)
    - validate: string enum=['DISABLED', 'ENABLED']
    - validateTimeoutInSeconds: integer (min=1, max=3600)
- environmentVariables: EnvironmentVariableMap (map)
  - map<EnvironmentVariableKey,EnvironmentVariableValue>
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
- clientToken: string (min=1, max=128)
**Output** UpdateMicrovmImageResponse:
- imageArn*: string (min=1, max=2048, pattern=[^\s]+)
- name*: string (min=1, max=64, pattern=[a-zA-Z0-9-_]+)
- state*: string enum=['CREATING', 'CREATED', 'CREATE_FAILED', 'UPDATING', 'UPDATED', 'UPDATE_FAILED', 'DELETING', 'DELETE_FAILED', 'DELETED']
- latestActiveImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- latestFailedImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- createdAt*: timestamp
- baseImageArn*: string (min=1, max=2048, pattern=[^\s]+)
- baseImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- buildRoleArn*: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- description: string
- codeArtifact*: CodeArtifact (structure)
  - uri: string (min=1, max=2048, pattern=[^\s]+)
- logging: Logging (structure)
  - disabled: LoggingDisabled (structure)
  - cloudWatch: CloudWatchLogging (structure)
    - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
    - logStream: string (min=1, max=512, pattern=[^:*]*)
- egressNetworkConnectors: UpdateMicrovmImageResponseEgressNetworkConnectorsList (list)
  - [list of string]
- cpuConfigurations: CpuConfigurationList (list)
  - [list of CpuConfiguration]
    - architecture*: string enum=['ARM_64']
- resources: ResourcesList (list)
  - [list of Resources]
    - minimumMemoryInMiB*: integer
- additionalOsCapabilities: CapabilityList (list)
  - [list of string enum=['ALL']]
- hooks: Hooks (structure)
  - port: integer (min=1, max=65535)
  - microvmHooks: MicrovmHooks (structure)
    - run: string enum=['DISABLED', 'ENABLED']
    - runTimeoutInSeconds: integer (min=1, max=60)
    - resume: string enum=['DISABLED', 'ENABLED']
    - resumeTimeoutInSeconds: integer (min=1, max=60)
    - suspend: string enum=['DISABLED', 'ENABLED']
    - suspendTimeoutInSeconds: integer (min=1, max=60)
    - terminate: string enum=['DISABLED', 'ENABLED']
    - terminateTimeoutInSeconds: integer (min=1, max=60)
  - microvmImageHooks: MicrovmImageHooks (structure)
    - ready: string enum=['DISABLED', 'ENABLED']
    - readyTimeoutInSeconds: integer (min=1, max=3600)
    - validate: string enum=['DISABLED', 'ENABLED']
    - validateTimeoutInSeconds: integer (min=1, max=3600)
- environmentVariables: EnvironmentVariableMap (map)
  - map<EnvironmentVariableKey,EnvironmentVariableValue>
- updatedAt*: timestamp
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException', 'ServiceQuotaExceededException']

## UpdateMicrovmImageVersion  `PATCH /2025-09-09/microvm-images/{imageIdentifier}/versions/{imageVersion}`
> <p>Updates the status of a specific MicroVM image version.</p>
**Input** UpdateMicrovmImageVersionRequest:
- imageIdentifier*: string (min=1, max=256) [uri:imageIdentifier]
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+) [uri:imageVersion]
- status*: string enum=['ACTIVE', 'INACTIVE']
**Output** UpdateMicrovmImageVersionResponse:
- baseImageArn*: string (min=1, max=2048, pattern=[^\s]+)
- baseImageVersion: string (min=1, max=2048, pattern=[^\s]+)
- buildRoleArn*: string (min=20, max=2048, pattern=arn:aws[a-z\-]*:iam::[0-9]{12}:role/?[a-zA-Z_0-9+=,.@\-_/]+)
- description: string
- codeArtifact*: CodeArtifact (structure)
  - uri: string (min=1, max=2048, pattern=[^\s]+)
- logging: Logging (structure)
  - disabled: LoggingDisabled (structure)
  - cloudWatch: CloudWatchLogging (structure)
    - logGroup: string (min=1, max=512, pattern=[a-zA-Z0-9_\-/.#]+)
    - logStream: string (min=1, max=512, pattern=[^:*]*)
- egressNetworkConnectors: UpdateMicrovmImageVersionResponseEgressNetworkConnectorsList (list)
  - [list of string]
- cpuConfigurations: CpuConfigurationList (list)
  - [list of CpuConfiguration]
    - architecture*: string enum=['ARM_64']
- resources: ResourcesList (list)
  - [list of Resources]
    - minimumMemoryInMiB*: integer
- additionalOsCapabilities: CapabilityList (list)
  - [list of string enum=['ALL']]
- hooks: Hooks (structure)
  - port: integer (min=1, max=65535)
  - microvmHooks: MicrovmHooks (structure)
    - run: string enum=['DISABLED', 'ENABLED']
    - runTimeoutInSeconds: integer (min=1, max=60)
    - resume: string enum=['DISABLED', 'ENABLED']
    - resumeTimeoutInSeconds: integer (min=1, max=60)
    - suspend: string enum=['DISABLED', 'ENABLED']
    - suspendTimeoutInSeconds: integer (min=1, max=60)
    - terminate: string enum=['DISABLED', 'ENABLED']
    - terminateTimeoutInSeconds: integer (min=1, max=60)
  - microvmImageHooks: MicrovmImageHooks (structure)
    - ready: string enum=['DISABLED', 'ENABLED']
    - readyTimeoutInSeconds: integer (min=1, max=3600)
    - validate: string enum=['DISABLED', 'ENABLED']
    - validateTimeoutInSeconds: integer (min=1, max=3600)
- environmentVariables: EnvironmentVariableMap (map)
  - map<EnvironmentVariableKey,EnvironmentVariableValue>
- imageArn*: string (min=1, max=2048, pattern=[^\s]+)
- imageVersion*: string (min=1, max=2048, pattern=[^\s]+)
- state*: string enum=['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED', 'DELETING', 'DELETED', 'DELETE_FAILED']
- status*: string enum=['ACTIVE', 'INACTIVE']
- createdAt*: timestamp
- updatedAt: timestamp
- stateReason: string
- tags: Tags (map)
  - map<TagKey,TagValue>
Errors: ['InternalServerException', 'AccessDeniedException', 'ResourceNotFoundException', 'ThrottlingException', 'ConflictException', 'ValidationException']

## All enums
- Architecture: ['ARM_64']
- BuildState: ['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED']
- Capability: ['ALL']
- Chipset: ['GRAVITON']
- HookState: ['DISABLED', 'ENABLED']
- MicrovmImageState: ['CREATING', 'CREATED', 'CREATE_FAILED', 'UPDATING', 'UPDATED', 'UPDATE_FAILED', 'DELETING', 'DELETE_FAILED', 'DELETED']
- MicrovmImageVersionState: ['PENDING', 'IN_PROGRESS', 'SUCCESSFUL', 'FAILED', 'DELETING', 'DELETED', 'DELETE_FAILED']
- MicrovmImageVersionStatus: ['ACTIVE', 'INACTIVE']
- MicrovmState: ['PENDING', 'RUNNING', 'SUSPENDING', 'SUSPENDED', 'TERMINATING', 'TERMINATED']