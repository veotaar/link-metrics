# Do not share runtime code between contenders

Contenders share only the OpenAPI contract, database migrations, generated derivatives, benchmark fixtures, and development configuration; they do not share handlers, validation logic, SQL helpers, authentication code, or domain services. Intentional duplication keeps each idiomatic runtime implementation inside the complete stack being measured instead of reducing contenders to adapters around a common implementation.
